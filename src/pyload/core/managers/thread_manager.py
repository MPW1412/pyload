# -*- coding: utf-8 -*-
# AUTHOR: RaNaN

from datetime import timedelta
import os
import re
import subprocess
import time

from random import choice
from threading import Event, Lock

import pycurl

from ..datatypes.pyfile import PyFile
from ..network.request_factory import getURL
from ..threads.decrypter_thread import DecrypterThread
from ..threads.download_thread import DownloadThread
from ..threads.info_thread import InfoThread
from ..utils import free_space, lock


class ThreadManager:
    """
    manages the download threads, assign jobs, reconnect etc.
    """

    def __init__(self, core):
        """
        Constructor.
        """
        self.pyload = core
        self._ = core._

        self.threads = []  #: thread list
        self.localThreads = []  #: addon+decrypter threads

        self.pause = True

        self.reconnecting = Event()
        self.reconnecting.clear()
        self.downloaded = 0  #: number of files downloaded since last cleanup

        self.lock = Lock()

        # some operations require to fetch url info from hoster, so we caching them so it wont be done twice
        # contains a timestamp and will be purged after timeout
        self.infoCache = {}

        # pool of ids for online check
        self.resultIDs = 0

        # threads which are fetching hoster results
        self.infoResults = {}
        # timeout for cache purge
        self.timestamp = 0

        pycurl.global_init(pycurl.GLOBAL_DEFAULT)

        for i in range(self.pyload.config.get("download", "max_downloads")):
            self.createThread()

    def createThread(self):
        """
        create a download thread.
        """
        thread = DownloadThread(self)
        self.threads.append(thread)

    def createInfoThread(self, data, pid):
        """
        start a thread whichs fetches online status and other infos
        data = [ .. () .. ]
        """
        self.timestamp = time.time() + timedelta(minutes=5).seconds

        InfoThread(self, data, pid)

    @lock
    def createResultThread(self, data, add=False):
        """
        creates a thread to fetch online status, returns result id.
        """
        self.timestamp = time.time() + timedelta(minutes=5).seconds

        rid = self.resultIDs
        self.resultIDs += 1

        InfoThread(self, data, rid=rid, add=add)

        return rid

    @lock
    def getInfoResult(self, rid):
        """
        returns result and clears it.
        """
        self.timestamp = time.time() + timedelta(minutes=5).seconds

        if rid in self.infoResults:
            data = self.infoResults[rid]
            self.infoResults[rid] = {}
            return data
        else:
            return {}

    @lock
    def setInfoResults(self, rid, result):
        self.infoResults[rid].update(result)

    def getActiveFiles(self):
        active = [
            x.active for x in self.threads if x.active and isinstance(x.active, PyFile)
        ]

        for t in self.localThreads:
            active.extend(t.getActiveFiles())

        return active

    def processingIds(self):
        """
        get a id list of all pyfiles processed.
        """
        return [x.id for x in self.getActiveFiles()]

    def run(self):
        """
        run all task which have to be done (this is for repetivive call by core)
        """
        try:
            self.tryReconnect()
        except Exception as exc:
            self.pyload.log.error(
                self._("Reconnect Failed: {}").format(exc),
                exc_info=self.pyload.debug > 1,
                stack_info=self.pyload.debug > 2,
            )
            self.reconnecting.clear()
        self.checkThreadCount()

        try:
            self.assignJob()
        except Exception as exc:
            self.pyload.log.warning(
                "Assign job error",
                exc,
                exc_info=self.pyload.debug > 1,
                stack_info=self.pyload.debug > 2,
            )

            time.sleep(0.5)
            self.assignJob()
            # it may be failed non critical so we try it again

        if (self.infoCache or self.infoResults) and self.timestamp < time.time():
            self.infoCache.clear()
            self.infoResults.clear()
            self.pyload.log.debug("Cleared Result cache")

    # ----------------------------------------------------------------------
    def tryReconnect(self):
        """
        checks if reconnect needed.
        """
        if not (
            self.pyload.config.get("reconnect", "enabled")
            and self.pyload.api.isTimeReconnect()
        ):
            return False

        active = [
            x.active.plugin.wantReconnect and x.active.plugin.waiting
            for x in self.threads
            if x.active
        ]

        if not (0 < active.count(True) == len(active)):
            return False

        reconnect_script = self.pyload.config.get("reconnect", "script")
        if not os.path.isfile(reconnect_script):
            self.pyload.config.set("reconnect", "enabled", False)
            self.pyload.log.warning(self._("Reconnect script not found!"))
            return

        self.reconnecting.set()

        # Do reconnect
        self.pyload.log.info(self._("Starting reconnect"))

        while [x.active.plugin.waiting for x in self.threads if x.active].count(
            True
        ) != 0:
            time.sleep(0.25)

        ip = self.getIP()

        self.pyload.addonManager.beforeReconnecting(ip)

        self.pyload.log.debug(f"Old IP: {ip}")

        try:
            reconn = subprocess.Popen(
                reconnect_script, bufsize=-1, shell=True
            )  #: , stdout=subprocess.PIPE)
        except Exception:
            self.pyload.log.warning(self._("Failed executing reconnect script!"))
            self.pyload.config.set("reconnect", "enabled", False)
            self.reconnecting.clear()
            return

        reconn.wait()
        time.sleep(1)
        ip = self.getIP()
        self.pyload.addonManager.afterReconnecting(ip)

        self.pyload.log.info(self._("Reconnected, new IP: {}").format(ip))

        self.reconnecting.clear()

    def getIP(self):
        """
        retrieve current ip.
        """
        services = [
            ("http://automation.whatismyip.com/n09230945.asp", "(\S+)"),
            ("http://checkip.dyndns.org/", ".*Current IP Address: (\S+)</body>.*"),
        ]

        ip = ""
        for i in range(10):
            try:
                sv = choice(services)
                ip = getURL(sv[0])
                ip = re.match(sv[1], ip).group(1)
                break
            except Exception:
                ip = ""
                time.sleep(1)

        return ip

    # ----------------------------------------------------------------------
    def checkThreadCount(self):
        """
        checks if there are need for increasing or reducing thread count.
        """
        if len(self.threads) == self.pyload.config.get("download", "max_downloads"):
            return True
        elif len(self.threads) < self.pyload.config.get("download", "max_downloads"):
            self.createThread()
        else:
            free = [x for x in self.threads if not x.active]
            if free:
                free[0].put("quit")

    def cleanPycurl(self):
        """
        make a global curl cleanup (currently ununused)
        """
        if self.processingIds():
            return False
        pycurl.global_cleanup()
        pycurl.global_init(pycurl.GLOBAL_DEFAULT)
        self.downloaded = 0
        self.pyload.log.debug("Cleaned up pycurl")
        return True

    # ----------------------------------------------------------------------
    def assignJob(self):
        """
        assing a job to a thread if possible.
        """
        if self.pause or not self.pyload.api.isTimeDownload():
            return

        # if self.downloaded > 20:
        #    if not self.cleanPyCurl(): return

        free = [x for x in self.threads if not x.active]

        inuse = set(
            [
                (x.active.pluginname, self.getLimit(x))
                for x in self.threads
                if x.active and x.active.hasPlugin() and x.active.plugin.account
            ]
        )
        inuse = [
            (
                x[0],
                x[1],
                len(
                    [
                        y
                        for y in self.threads
                        if y.active and y.active.pluginname == x[0]
                    ]
                ),
            )
            for x in inuse
        ]
        onlimit = [x[0] for x in inuse if x[1] > 0 and x[2] >= x[1]]

        occ = sorted(
            [
                x.active.pluginname
                for x in self.threads
                if x.active and x.active.hasPlugin() and not x.active.plugin.multiDL
            ]
            + onlimit
        )

        occ = tuple(set(occ))
        job = self.pyload.files.getJob(occ)
        if job:
            try:
                job.initPlugin()
            except Exception as exc:
                self.pyload.log.critical(
                    exc, exc_info=True, stack_info=self.pyload.debug > 2
                )
                job.setStatus("failed")
                job.error = str(exc)
                job.release()
                return

            if job.plugin.__type__ == "downloader":
                spaceLeft = (
                    free_space(self.pyload.config.get("general", "storage_folder"))
                    >> 20
                )
                if spaceLeft < self.pyload.config.get("general", "min_free_space"):
                    self.pyload.log.warning(self._("Not enough space left on device"))
                    self.pause = True

                if free and not self.pause:
                    thread = free[0]
                    # self.downloaded += 1

                    thread.put(job)
                else:
                    # put job back
                    if occ not in self.pyload.files.jobCache:
                        self.pyload.files.jobCache[occ] = []
                    self.pyload.files.jobCache[occ].append(job.id)

                    # check for decrypt jobs
                    job = self.pyload.files.getDecryptJob()
                    if job:
                        job.initPlugin()
                        thread = DecrypterThread(self, job)

            else:
                thread = DecrypterThread(self, job)

    def getLimit(self, thread):
        limit = thread.active.plugin.account.getAccountData(thread.active.plugin.user)[
            "options"
        ].get("limitDL", ["0"])[0]
        return int(limit)

    def cleanup(self):
        """
        do global cleanup, should be called when finished with pycurl.
        """
        pycurl.global_cleanup()
