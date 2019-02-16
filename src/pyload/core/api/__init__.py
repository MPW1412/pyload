# -*- coding: utf-8 -*-
#      ____________
#   _ /       |    \ ___________ _ _______________ _ ___ _______________
#  /  |    ___/    |   _ __ _  _| |   ___  __ _ __| |   \\    ___  ___ _\
# /   \___/  ______/  | '_ \ || | |__/ _ \/ _` / _` |    \\  / _ \/ _ `/ \
# \       |   o|      | .__/\_, |____\___/\__,_\__,_|    // /_//_/\_, /  /
#  \______\    /______|_|___|__/________________________//______ /___/__/
#          \  /
#           \/

import re

import os
import time

from ..datatypes.pyfile import PyFile
from ..network.request_factory import getURL
from ..utils.packagetools import parseNames
from ..utils import compare_time, free_space
import json
from enum import IntFlag

from ..datatypes import *

# contains function names mapped to their permissions
# unlisted functions are for admins only
permMap = {}

# decorator only called on init, never initialized, so has no effect on runtime
def permission(bits):
    class Wrapper:
        def __new__(cls, func, *args, **kwargs):
            permMap[func.__name__] = bits
            return func

    return Wrapper


urlmatcher = re.compile(
    r"((https?|ftps?|xdcc|sftp):((//)|(\\\\))+[\w\d:#@%/;$()~_?\+\-=\\\.&]*)",
    re.IGNORECASE,
)


class Perms(IntFlag):
    ALL = 0  #: requires no permission, but login
    ADD = 1  #: can add packages
    DELETE = 2  #: can delete packages
    STATUS = 4  #: see and change server status
    LIST = 16  #: see queue and collector
    MODIFY = 32  #: moddify some attribute of downloads
    DOWNLOAD = 64  #: can download from webinterface
    SETTINGS = 128  #: can access settings
    ACCOUNTS = 256  #: can access accounts
    LOGS = 512  #: can see server logs


class Role(IntFlag):
    ADMIN = 0  #: admin has all permissions implicit
    USER = 1


def has_permission(userperms, perms):
    # bytewise or perms before if needed
    return perms == (userperms & perms)


# API VERSION
__version__ = 1


class Api:
    """
    **pyLoads API**

    This is accessible either internal via core.api or via thrift backend.

    see Thrift specification file remote/thriftbackend/pyload.thrift\
    for information about data structures and what methods are usuable with rpc.

    Most methods requires specific permissions, please look at the source code if you need to know.\
    These can be configured via webinterface.
    Admin user have all permissions, and are the only ones who can access the methods with no specific permission.
    """

    def __init__(self, core):
        self.pyload = core
        self._ = core._

    def _convertPyFile(self, p):
        f = FileData(
            p["id"],
            p["url"],
            p["name"],
            p["plugin"],
            p["size"],
            p["format_size"],
            p["status"],
            p["statusmsg"],
            p["package"],
            p["error"],
            p["order"],
        )
        return f

    def _convertConfigFormat(self, c):
        sections = {}
        for sectionName, sub in c.items():
            section = ConfigSection(sectionName, sub["desc"])
            items = []
            for key, data in sub.items():
                if key in ("desc", "outline"):
                    continue
                item = ConfigItem()
                item.name = key
                item.description = data["desc"]
                item.value = (
                    str(data["value"])
                    if not isinstance(data["value"], str)
                    else data["value"]
                )
                item.type = data["type"]
                items.append(item)
            section.items = items
            sections[sectionName] = section
            if "outline" in sub:
                section.outline = sub["outline"]
        return sections

    @permission(Perms.SETTINGS)
    def getConfigValue(self, category, option, section="core"):
        """
        Retrieve config value.

        :param category: name of category, or plugin
        :param option: config option
        :param section: 'plugin' or 'core'
        :return: config value as string
        """
        if section == "core":
            value = self.pyload.config[category][option]
        else:
            value = self.pyload.config.getPlugin(category, option)

        return str(value) if not isinstance(value, str) else value

    @permission(Perms.SETTINGS)
    def setConfigValue(self, category, option, value, section="core"):
        """
        Set new config value.

        :param category:
        :param option:
        :param value: new config value
        :param section: 'plugin' or 'core
        """
        self.pyload.addonManager.dispatchEvent(
            "configChanged", category, option, value, section
        )

        if section == "core":
            self.pyload.config[category][option] = value

            if option in (
                "limit_speed",
                "max_speed",
            ):  #: not so nice to update the limit
                self.pyload.requestFactory.updateBucket()

        elif section == "plugin":
            self.pyload.config.setPlugin(category, option, value)

    @permission(Perms.SETTINGS)
    def getConfig(self):
        """
        Retrieves complete config of core.

        :return: list of `ConfigSection`
        """
        return self._convertConfigFormat(self.pyload.config.config)

    def getConfigDict(self):
        """
        Retrieves complete config in dict format, not for RPC.

        :return: dict
        """
        return self.pyload.config.config

    @permission(Perms.SETTINGS)
    def getPluginConfig(self):
        """
        Retrieves complete config for all plugins.

        :return: list of `ConfigSection`
        """
        return self._convertConfigFormat(self.pyload.config.plugin)

    def getPluginConfigDict(self):
        """
        Plugin config as dict, not for RPC.

        :return: dict
        """
        return self.pyload.config.plugin

    @permission(Perms.STATUS)
    def pauseServer(self):
        """
        Pause server: Tt wont start any new downloads, but nothing gets aborted.
        """
        self.pyload.threadManager.pause = True

    @permission(Perms.STATUS)
    def unpauseServer(self):
        """
        Unpause server: New Downloads will be started.
        """
        self.pyload.threadManager.pause = False

    @permission(Perms.STATUS)
    def togglePause(self):
        """
        Toggle pause state.

        :return: new pause state
        """
        self.pyload.threadManager.pause ^= True
        return self.pyload.threadManager.pause

    @permission(Perms.STATUS)
    def toggleReconnect(self):
        """
        Toggle reconnect activation.

        :return: new reconnect state
        """
        self.pyload.config.toggle("reconnect", "enabled")
        return self.pyload.config.get("reconnect", "enabled")

    @permission(Perms.LIST)
    def statusServer(self):
        """
        Some general information about the current status of pyLoad.

        :return: `ServerStatus`
        """
        serverStatus = ServerStatus(
            self.pyload.threadManager.pause,
            len(self.pyload.threadManager.processingIds()),
            self.pyload.files.getQueueCount(),
            self.pyload.files.getFileCount(),
            0,
            not self.pyload.threadManager.pause and self.isTimeDownload(),
            self.pyload.config.get("reconnect", "enabled") and self.isTimeReconnect(),
            self.isCaptchaWaiting(),
        )

        for pyfile in [
            x.active
            for x in self.pyload.threadManager.threads
            if x.active and isinstance(x.active, PyFile)
        ]:
            serverStatus.speed += pyfile.getSpeed()  #: bytes/s

        return serverStatus

    @permission(Perms.STATUS)
    def freeSpace(self):
        """
        Available free space at download directory in bytes.
        """
        return free_space(self.pyload.config.get("general", "storage_folder"))

    @permission(Perms.ALL)
    def getServerVersion(self):
        """
        pyLoad Core version.
        """
        return self.pyload.version

    def kill(self):
        """
        Clean way to quit pyLoad.
        """
        self.pyload._do_exit = True

    def restart(self):
        """
        Restart pyload core.
        """
        self.pyload._do_restart = True

    @permission(Perms.LOGS)
    def getLog(self, offset=0):
        """
        Returns most recent log entries.

        :param offset: line offset
        :return: List of log entries
        """
        filename = os.path.join(
            self.pyload.config.get("log", "filelog_folder"), "log.txt"
        )
        try:
            with open(filename) as fh:
                lines = fh.readlines()
            if offset >= len(lines):
                return []
            return lines[offset:]
        except Exception:
            return ["No log available"]

    @permission(Perms.STATUS)
    def isTimeDownload(self):
        """
        Checks if pyload will start new downloads according to time in config.

        :return: bool
        """
        start = self.pyload.config.get("download", "start_time").split(":")
        end = self.pyload.config.get("download", "end_time").split(":")
        return compare_time(start, end)

    @permission(Perms.STATUS)
    def isTimeReconnect(self):
        """
        Checks if pyload will try to make a reconnect.

        :return: bool
        """
        start = self.pyload.config.get("reconnect", "start_time").split(":")
        end = self.pyload.config.get("reconnect", "end_time").split(":")
        return compare_time(start, end) and self.pyload.config.get(
            "reconnect", "enabled"
        )

    @permission(Perms.LIST)
    def statusDownloads(self):
        """
        Status off all currently running downloads.

        :return: list of `DownloadStatus`
        """
        data = []
        for pyfile in self.pyload.threadManager.getActiveFiles():
            if not isinstance(pyfile, PyFile):
                continue

            data.append(
                DownloadInfo(
                    pyfile.id,
                    pyfile.name,
                    pyfile.getSpeed(),
                    pyfile.getETA(),
                    pyfile.formatETA(),
                    pyfile.getBytesLeft(),
                    pyfile.getSize(),
                    pyfile.formatSize(),
                    pyfile.getPercent(),
                    pyfile.status,
                    pyfile.getStatusName(),
                    pyfile.formatWait(),
                    pyfile.waitUntil,
                    pyfile.packageid,
                    pyfile.package().name,
                    pyfile.pluginname,
                )
            )

        return data

    @permission(Perms.ADD)
    def addPackage(self, name, links, dest=Destination.QUEUE.value):
        """
        Adds a package, with links to desired destination.

        :param name: name of the new package
        :param links: list of urls
        :param dest: `Destination`
        :return: package id of the new package
        """
        if self.pyload.config.get("general", "folder_per_package"):
            folder = name
        else:
            folder = ""

        folder = (
            folder.replace("http://", "")
            .replace(":", "")
            .replace("/", "_")
            .replace("\\", "_")
        )

        pid = self.pyload.files.addPackage(name, folder, Destination(dest))

        self.pyload.files.addLinks(links, pid)

        self.pyload.log.info(
            self._("Added package {name} containing {count:d} links").format(
                name=name, count=len(links)
            )
        )

        self.pyload.files.save()

        return pid

    @permission(Perms.ADD)
    def parseURLs(self, html=None, url=None):
        """
        Parses html content or any arbitaty text for links and returns result of
        `checkURLs`

        :param html: html source
        :return:
        """
        urls = []

        if html:
            urls += [x[0] for x in urlmatcher.findall(html)]

        if url:
            page = getURL(url)
            urls += [x[0] for x in urlmatcher.findall(page)]

        # remove duplicates
        return self.checkURLs(set(urls))

    @permission(Perms.ADD)
    def checkURLs(self, urls):
        """
        Gets urls and returns pluginname mapped to list of matches urls.

        :param urls:
        :return: {plugin: urls}
        """
        data = self.pyload.pluginManager.parseUrls(urls)
        plugins = {}

        for url, plugin in data:
            if plugin in plugins:
                plugins[plugin].append(url)
            else:
                plugins[plugin] = [url]

        return plugins

    @permission(Perms.ADD)
    def checkOnlineStatus(self, urls):
        """
        initiates online status check.

        :param urls:
        :return: initial set of data as `OnlineCheck` instance containing the result id
        """
        data = self.pyload.pluginManager.parseUrls(urls)

        rid = self.pyload.threadManager.createResultThread(data, False)

        tmp = [
            (url, (url, OnlineStatus(url, pluginname, "unknown", 3, 0)))
            for url, pluginname in data
        ]
        data = parseNames(tmp)
        result = {}

        for k, v in data.items():
            for url, status in v:
                status.packagename = k
                result[url] = status

        return OnlineCheck(rid, result)

    @permission(Perms.ADD)
    def checkOnlineStatusContainer(self, urls, container, data):
        """
        checks online status of urls and a submited container file.

        :param urls: list of urls
        :param container: container file name
        :param data: file content
        :return: online check
        """
        with open(
            os.path.join(
                self.pyload.config.get("general", "storage_folder"), "tmp_" + container
            ),
            "wb",
        ) as th:
            th.write(data)

        return self.checkOnlineStatus(urls + [th.name])

    @permission(Perms.ADD)
    def pollResults(self, rid):
        """
        Polls the result available for ResultID.

        :param rid: `ResultID`
        :return: `OnlineCheck`, if rid is -1 then no more data available
        """
        result = self.pyload.threadManager.getInfoResult(rid)

        if "ALL_INFO_FETCHED" in result:
            del result["ALL_INFO_FETCHED"]
            return OnlineCheck(-1, result)
        else:
            return OnlineCheck(rid, result)

    @permission(Perms.ADD)
    def generatePackages(self, links):
        """
        Parses links, generates packages names from urls.

        :param links: list of urls
        :return: package names mapped to urls
        """
        result = parseNames((x, x) for x in links)
        return result

    @permission(Perms.ADD)
    def generateAndAddPackages(self, links, dest=Destination.QUEUE.value):
        """
        Generates and add packages.

        :param links: list of urls
        :param dest: `Destination`
        :return: list of package ids
        """
        return [
            self.addPackage(name, urls, dest)
            for name, urls in self.generatePackages(links).items()
        ]

    @permission(Perms.ADD)
    def checkAndAddPackages(self, links, dest=Destination.QUEUE.value):
        """
        Checks online status, retrieves names, and will add packages.\ Because of this
        packages are not added immediatly, only for internal use.

        :param links: list of urls
        :param dest: `Destination`
        :return: None
        """
        data = self.pyload.pluginManager.parseUrls(links)
        self.pyload.threadManager.createResultThread(data, True)

    @permission(Perms.LIST)
    def getPackageData(self, pid):
        """
        Returns complete information about package, and included files.

        :param pid: package id
        :return: `PackageData` with .links attribute
        """
        data = self.pyload.files.getPackageData(int(pid))

        if not data:
            raise PackageDoesNotExists(pid)

        pdata = PackageData(
            data["id"],
            data["name"],
            data["folder"],
            data["site"],
            data["password"],
            data["queue"],
            data["order"],
            links=[self._convertPyFile(x) for x in data["links"].values()],
        )

        return pdata

    @permission(Perms.LIST)
    def getPackageInfo(self, pid):
        """
        Returns information about package, without detailed information about containing
        files.

        :param pid: package id
        :return: `PackageData` with .fid attribute
        """
        data = self.pyload.files.getPackageData(int(pid))

        if not data:
            raise PackageDoesNotExists(pid)

        pdata = PackageData(
            data["id"],
            data["name"],
            data["folder"],
            data["site"],
            data["password"],
            data["queue"],
            data["order"],
            fids=[int(x) for x in data["links"]],
        )

        return pdata

    @permission(Perms.LIST)
    def getFileData(self, fid):
        """
        Get complete information about a specific file.

        :param fid: file id
        :return: `FileData`
        """
        info = self.pyload.files.getFileData(int(fid))
        if not info:
            raise FileDoesNotExists(fid)

        fileinfo = list(info.values())[0]
        fdata = self._convertPyFile(fileinfo)
        return fdata

    @permission(Perms.DELETE)
    def deleteFiles(self, fids):
        """
        Deletes several file entries from pyload.

        :param fids: list of file ids
        """
        for id in fids:
            self.pyload.files.deleteLink(int(id))

        self.pyload.files.save()

    @permission(Perms.DELETE)
    def deletePackages(self, pids):
        """
        Deletes packages and containing links.

        :param pids: list of package ids
        """
        for id in pids:
            self.pyload.files.deletePackage(int(id))

        self.pyload.files.save()

    @permission(Perms.LIST)
    def getQueue(self):
        """
        Returns info about queue and packages, **not** about files, see `getQueueData` \
        or `getPackageData` instead.

        :return: list of `PackageInfo`
        """
        return [
            PackageData(
                pack["id"],
                pack["name"],
                pack["folder"],
                pack["site"],
                pack["password"],
                pack["queue"],
                pack["order"],
                pack["linksdone"],
                pack["sizedone"],
                pack["sizetotal"],
                pack["linkstotal"],
            )
            for pack in self.pyload.files.getInfoData(Destination.QUEUE).values()
        ]

    @permission(Perms.LIST)
    def getQueueData(self):
        """
        Return complete data about everything in queue, this is very expensive use it
        sparely.\ See `getQueue` for alternative.

        :return: list of `PackageData`
        """
        return [
            PackageData(
                pack["id"],
                pack["name"],
                pack["folder"],
                pack["site"],
                pack["password"],
                pack["queue"],
                pack["order"],
                pack["linksdone"],
                pack["sizedone"],
                pack["sizetotal"],
                links=[self._convertPyFile(x) for x in pack["links"].values()],
            )
            for pack in self.pyload.files.getCompleteData(Destination.QUEUE).values()
        ]

    @permission(Perms.LIST)
    def getCollector(self):
        """
        same as `getQueue` for collector.

        :return: list of `PackageInfo`
        """
        return [
            PackageData(
                pack["id"],
                pack["name"],
                pack["folder"],
                pack["site"],
                pack["password"],
                pack["queue"],
                pack["order"],
                pack["linksdone"],
                pack["sizedone"],
                pack["sizetotal"],
                pack["linkstotal"],
            )
            for pack in self.pyload.files.getInfoData(Destination.COLLECTOR).values()
        ]

    @permission(Perms.LIST)
    def getCollectorData(self):
        """
        same as `getQueueData` for collector.

        :return: list of `PackageInfo`
        """
        return [
            PackageData(
                pack["id"],
                pack["name"],
                pack["folder"],
                pack["site"],
                pack["password"],
                pack["queue"],
                pack["order"],
                pack["linksdone"],
                pack["sizedone"],
                pack["sizetotal"],
                links=[self._convertPyFile(x) for x in pack["links"].values()],
            )
            for pack in self.pyload.files.getCompleteData(
                Destination.COLLECTOR.value
            ).values()
        ]

    @permission(Perms.ADD)
    def addFiles(self, pid, links):
        """
        Adds files to specific package.

        :param pid: package id
        :param links: list of urls
        """
        self.pyload.files.addLinks(links, int(pid))

        self.pyload.log.info(
            self._("Added {count:d} links to package #{package:d} ").format(
                count=len(links), package=pid
            )
        )
        self.pyload.files.save()

    @permission(Perms.MODIFY)
    def pushToQueue(self, pid):
        """
        Moves package from Collector to Queue.

        :param pid: package id
        """
        self.pyload.files.setPackageLocation(pid, Destination.QUEUE)

    @permission(Perms.MODIFY)
    def pullFromQueue(self, pid):
        """
        Moves package from Queue to Collector.

        :param pid: package id
        """
        self.pyload.files.setPackageLocation(pid, Destination.COLLECTOR)

    @permission(Perms.MODIFY)
    def restartPackage(self, pid):
        """
        Restarts a package, resets every containing files.

        :param pid: package id
        """
        self.pyload.files.restartPackage(int(pid))

    @permission(Perms.MODIFY)
    def restartFile(self, fid):
        """
        Resets file status, so it will be downloaded again.

        :param fid:  file id
        """
        self.pyload.files.restartFile(int(fid))

    @permission(Perms.MODIFY)
    def recheckPackage(self, pid):
        """
        Proofes online status of all files in a package, also a default action when
        package is added.

        :param pid:
        :return:
        """
        self.pyload.files.reCheckPackage(int(pid))

    @permission(Perms.MODIFY)
    def stopAllDownloads(self):
        """
        Aborts all running downloads.
        """
        pyfiles = self.pyload.files.cache.values()
        for pyfile in pyfiles:
            pyfile.abortDownload()

    @permission(Perms.MODIFY)
    def stopDownloads(self, fids):
        """
        Aborts specific downloads.

        :param fids: list of file ids
        :return:
        """
        pyfiles = self.pyload.files.cache.values()
        for pyfile in pyfiles:
            if pyfile.id in fids:
                pyfile.abortDownload()

    @permission(Perms.MODIFY)
    def setPackageName(self, pid, name):
        """
        Renames a package.

        :param pid: package id
        :param name: new package name
        """
        pack = self.pyload.files.getPackage(pid)
        pack.name = name
        pack.sync()

    @permission(Perms.MODIFY)
    def movePackage(self, destination, pid):
        """
        Set a new package location.

        :param destination: `Destination`
        :param pid: package id
        """
        try:
            dest = Destination(destination)
        except ValueError:
            pass
        else:
            self.pyload.files.setPackageLocation(pid, dest)

    @permission(Perms.MODIFY)
    def moveFiles(self, fids, pid):
        """
        Move multiple files to another package.

        :param fids: list of file ids
        :param pid: destination package
        :return:
        """
        # TODO: implement
        pass

    @permission(Perms.ADD)
    def uploadContainer(self, filename, data):
        """
        Uploads and adds a container file to pyLoad.

        :param filename: filename, extension is important so it can correctly decrypted
        :param data: file content
        """
        with open(
            os.path.join(
                self.pyload.config.get("general", "storage_folder"), "tmp_" + filename
            ),
            "wb",
        ) as th:
            th.write(data)

        self.addPackage(th.name, [th.name], Destination.QUEUE.value)

    @permission(Perms.MODIFY)
    def orderPackage(self, pid, position):
        """
        Gives a package a new position.

        :param pid: package id
        :param position:
        """
        self.pyload.files.reorderPackage(pid, position)

    @permission(Perms.MODIFY)
    def orderFile(self, fid, position):
        """
        Gives a new position to a file within its package.

        :param fid: file id
        :param position:
        """
        self.pyload.files.reorderFile(fid, position)

    @permission(Perms.MODIFY)
    def setPackageData(self, pid, data):
        """
        Allows to modify several package attributes.

        :param pid: package id
        :param data: dict that maps attribute to desired value
        """
        p = self.pyload.files.getPackage(pid)
        if not p:
            raise PackageDoesNotExists(pid)

        for key, value in data.items():
            if key == "id":
                continue
            setattr(p, key, value)

        p.sync()
        self.pyload.files.save()

    @permission(Perms.DELETE)
    def deleteFinished(self):
        """
        Deletes all finished files and completly finished packages.

        :return: list of deleted package ids
        """
        return self.pyload.files.deleteFinishedLinks()

    @permission(Perms.MODIFY)
    def restartFailed(self):
        """
        Restarts all failed failes.
        """
        self.pyload.files.restartFailed()

    @permission(Perms.LIST)
    def getPackageOrder(self, destination):
        """
        Returns information about package order.

        :param destination: `Destination`
        :return: dict mapping order to package id
        """
        packs = self.pyload.files.getInfoData(Destination(destination))
        order = {}

        for pid in packs:
            pack = self.pyload.files.getPackageData(int(pid))
            while pack["order"] in order.keys():  #: just in case
                pack["order"] += 1
            order[pack["order"]] = pack["id"]
        return order

    @permission(Perms.LIST)
    def getFileOrder(self, pid):
        """
        Information about file order within package.

        :param pid:
        :return: dict mapping order to file id
        """
        rawData = self.pyload.files.getPackageData(int(pid))
        order = {}
        for id, pyfile in rawData["links"].items():
            while pyfile["order"] in order.keys():  #: just in case
                pyfile["order"] += 1
            order[pyfile["order"]] = pyfile["id"]
        return order

    @permission(Perms.STATUS)
    def isCaptchaWaiting(self):
        """
        Indicates wether a captcha task is available.

        :return: bool
        """
        self.pyload.lastClientConnected = time.time()
        task = self.pyload.captchaManager.getTask()
        return task is not None

    @permission(Perms.STATUS)
    def getCaptchaTask(self, exclusive=False):
        """
        Returns a captcha task.

        :param exclusive: unused
        :return: `CaptchaTask`
        """
        self.pyload.lastClientConnected = time.time()
        task = self.pyload.captchaManager.getTask()
        if task:
            task.setWatingForUser(exclusive=exclusive)
            data, type, result = task.getCaptcha()
            t = CaptchaTask(int(task.id), json.dumps(data), type, result)
            return t
        else:
            return CaptchaTask(-1)

    @permission(Perms.STATUS)
    def getCaptchaTaskStatus(self, tid):
        """
        Get information about captcha task.

        :param tid: task id
        :return: string
        """
        self.pyload.lastClientConnected = time.time()
        t = self.pyload.captchaManager.getTaskByID(tid)
        return t.getStatus() if t else ""

    @permission(Perms.STATUS)
    def setCaptchaResult(self, tid, result):
        """
        Set result for a captcha task.

        :param tid: task id
        :param result: captcha result
        """
        self.pyload.lastClientConnected = time.time()
        task = self.pyload.captchaManager.getTaskByID(tid)
        if task:
            task.setResult(result)
            self.pyload.captchaManager.removeTask(task)

    @permission(Perms.STATUS)
    def getEvents(self, uuid):
        """
        Lists occured events, may be affected to changes in future.

        :param uuid:
        :return: list of `Events`
        """
        events = self.pyload.eventManager.getEvents(uuid)
        newEvents = []

        def convDest(d):
            return (Destination.QUEUE if d == "queue" else Destination.COLLECTOR).value

        for e in events:
            event = EventInfo()
            event.eventname = e[0]
            if e[0] in ("update", "remove", "insert"):
                event.id = e[3]
                event.type = (
                    ElementType.PACKAGE if e[2] == "pack" else ElementType.FILE
                ).value
                event.destination = convDest(e[1])
            elif e[0] == "order":
                if e[1]:
                    event.id = e[1]
                    event.type = (
                        ElementType.PACKAGE if e[2] == "pack" else ElementType.FILE
                    )
                    event.destination = convDest(e[3])
            elif e[0] == "reload":
                event.destination = convDest(e[1])
            newEvents.append(event)
        return newEvents

    @permission(Perms.ACCOUNTS)
    def getAccounts(self, refresh):
        """
        Get information about all entered accounts.

        :param refresh: reload account info
        :return: list of `AccountInfo`
        """
        accs = self.pyload.accountManager.getAccountInfos(False, refresh)
        accounts = []
        for group in accs.values():
            accounts.extend(
                [
                    AccountInfo(
                        acc["validuntil"],
                        acc["login"],
                        acc["options"],
                        acc["valid"],
                        acc["trafficleft"],
                        acc["maxtraffic"],
                        acc["premium"],
                        acc["type"],
                    )
                    for acc in group
                ]
            )
        return accounts

    @permission(Perms.ALL)
    def getAccountTypes(self):
        """
        All available account types.

        :return: list
        """
        return list(self.pyload.accountManager.accounts.keys())

    @permission(Perms.ACCOUNTS)
    def updateAccount(self, plugin, account, password=None, options={}):
        """
        Changes pw/options for specific account.
        """
        self.pyload.accountManager.updateAccount(plugin, account, password, options)

    @permission(Perms.ACCOUNTS)
    def removeAccount(self, plugin, account):
        """
        Remove account from pyload.

        :param plugin: pluginname
        :param account: accountname
        """
        self.pyload.accountManager.removeAccount(plugin, account)

    @permission(Perms.ALL)
    def login(self, username, password):
        """
        Login into pyLoad, this **must** be called when using rpc before any methods can
        be used.

        :param username:
        :param password:
        :param remoteip: Omit this argument, its only used internal
        :return: bool indicating login was successful
        """
        return True if self.checkAuth(username, password) else False

    def checkAuth(self, username, password):
        """
        Check authentication and returns details.

        :param username:
        :param password:
        :param remoteip:
        :return: dict with info, empty when login is incorrect
        """
        return self.pyload.db.checkAuth(username, password)

    def isAuthorized(self, func, userdata):
        """
        checks if the user is authorized for specific method.

        :param func: function name
        :param userdata: dictionary of user data
        :return: boolean
        """
        if userdata["role"] == Role.ADMIN:
            return True
        elif func in permMap and has_permission(userdata["permission"], permMap[func]):
            return True
        else:
            return False

    # TODO: add security permission check
    # remove?
    def get_userdir(self):
        return os.path.realpath(self.pyload.userdir)

    # TODO: add security permission check
    # remove?
    def get_cachedir(self):
        return os.path.realpath(self.pyload.cachedir)

    #: Old API
    @permission(Perms.ALL)
    def getUserData(self, username, password):
        """
        similar to `checkAuth` but returns UserData thrift type.
        """
        user = self.checkAuth(username, password)
        if user:
            return OldUserData(
                user["name"],
                user["email"],
                user["role"],
                user["permission"],
                user["template"],
            )
        else:
            return OldUserData()

    @permission(Perms.ALL)
    def get_userdata(self, username, password):
        """
        similar to `checkAuth` but returns UserData thrift type.
        """
        user = self.checkAuth(username, password)
        if user:
            return UserData(
                user["id"],
                user["name"],
                user["email"],
                user["role"],
                user["permission"],
                user["template"],
            )
        else:
            return UserData()

    #: Old API
    def getAllUserData(self):
        """
        returns all known user and info.
        """
        res = {}
        for id, data in self.pyload.db.getAllUserData().items():
            res[data["name"]] = OldUserData(
                data["name"],
                data["email"],
                data["role"],
                data["permission"],
                data["template"],
            )

        return res

    def get_all_userdata(self):
        """
        returns all known user and info.
        """
        res = {}
        for id, data in self.pyload.db.getAllUserData().items():
            res[id] = UserData(
                id,
                data["name"],
                data["email"],
                data["role"],
                data["permission"],
                data["template"],
            )
        return res

    @permission(Perms.STATUS)
    def getServices(self):
        """
        A dict of available services, these can be defined by addon plugins.

        :return: dict with this style: {"plugin": {"method": "description"}}
        """
        data = {}
        for plugin, funcs in self.pyload.addonManager.methods.items():
            data[plugin] = funcs

        return data

    @permission(Perms.STATUS)
    def hasService(self, plugin, func):
        """
        Checks wether a service is available.

        :param plugin:
        :param func:
        :return: bool
        """
        cont = self.pyload.addonManager.methods
        return plugin in cont and func in cont[plugin]

    @permission(Perms.STATUS)
    def call(self, info):
        """
        Calls a service (a method in addon plugin).

        :param info: `ServiceCall`
        :return: result
        :raises: ServiceDoesNotExists, when its not available
        :raises: ServiceException, when a exception was raised
        """
        plugin = info.plugin
        func = info.func
        args = info.arguments
        parse = info.parseArguments

        if not self.hasService(plugin, func):
            raise ServiceDoesNotExists(plugin, func)

        try:
            ret = self.pyload.addonManager.callRPC(plugin, func, args, parse)
            return str(ret)
        except Exception as exc:
            raise ServiceException(exc)

    @permission(Perms.STATUS)
    def getAllInfo(self):
        """
        Returns all information stored by addon plugins. Values are always strings.

        :return: {"plugin": {"name": value } }
        """
        return self.pyload.addonManager.getAllInfo()

    @permission(Perms.STATUS)
    def getInfoByPlugin(self, plugin):
        """
        Returns information stored by a specific plugin.

        :param plugin: pluginname
        :return: dict of attr names mapped to value {"name": value}
        """
        return self.pyload.addonManager.getInfo(plugin)

    def changePassword(self, user, oldpw, newpw):
        """
        changes password for specific user.
        """
        return self.pyload.db.changePassword(user, oldpw, newpw)

    def setUserPermission(self, user, permission, role):
        self.pyload.db.setPermission(user, permission)
        self.pyload.db.setRole(user, role)
