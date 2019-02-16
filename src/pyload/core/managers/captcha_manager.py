# -*- coding: utf-8 -*-
# AUTHOR: mkaay, RaNaN

import time

from threading import Lock

from ..utils import lock


class CaptchaManager:
    def __init__(self, core):
        self.lock = Lock()
        self.pyload = core
        self._ = core._
        self.tasks = []  #: Task store, for outgoing tasks only

        self.ids = 0  #: Only for internal purpose

    def newTask(self, format, params, result_type):
        task = CaptchaTask(self.ids, format, params, result_type)
        self.ids += 1
        return task

    @lock
    def removeTask(self, task):
        if task in self.tasks:
            self.tasks.remove(task)

    @lock
    def getTask(self):
        for task in self.tasks:
            if task.status in ("waiting", "shared-user"):
                return task
        return None

    @lock
    def getTaskByID(self, tid):
        for task in self.tasks:
            if task.id == str(tid):  #: Task ids are strings
                return task
        return None

    def handleCaptcha(self, task, timeout):
        cli = self.pyload.isClientConnected()

        task.setWaiting(timeout)

        # if cli:  #: Client connected -> should solve the captcha
        #     task.setWaiting(50)  #: Wait minimum 50 sec for response

        for plugin in self.pyload.addonManager.activePlugins():
            try:
                plugin.newCaptchaTask(task)
            except Exception:
                pass

        if task.handler or cli:  #: The captcha was handled
            self.tasks.append(task)
            return True

        task.error = self._("No Client connected for captcha decrypting")

        return False


class CaptchaTask:
    def __init__(self, id, format, params={}, result_type="textual"):
        self.id = str(id)
        self.captchaParams = params
        self.captchaFormat = format
        self.captchaResultType = result_type
        self.handler = []  #: the addon plugins that will take care of the solution
        self.result = None
        self.waitUntil = None
        self.error = None  #: error message

        self.status = "init"
        self.data = {}  #: handler can store data here

    def getCaptcha(self):
        return self.captchaParams, self.captchaFormat, self.captchaResultType

    def setResult(self, result):
        if self.isTextual() or self.isInteractive():
            self.result = result

        elif self.isPositional():
            try:
                parts = result.split(",")
                self.result = (int(parts[0]), int(parts[1]))
            except Exception:
                self.result = None

    def getResult(self):
        return self.result

    def getStatus(self):
        return self.status

    def setWaiting(self, sec):
        """
        let the captcha wait secs for the solution.
        """
        self.waitUntil = max(time.time() + sec, self.waitUntil)
        self.status = "waiting"

    def isWaiting(self):
        if self.result or self.error or time.time() > self.waitUntil:
            return False

        return True

    def isTextual(self):
        """
        returns if text is written on the captcha.
        """
        return self.captchaResultType == "textual"

    def isPositional(self):
        """
        returns if user have to click a specific region on the captcha.
        """
        return self.captchaResultType == "positional"

    def isInteractive(self):
        """
        returns if user has to solve the captcha in an interactive iframe.
        """
        return self.captchaResultType == "interactive"

    def setWatingForUser(self, exclusive):
        if exclusive:
            self.status = "user"
        else:
            self.status = "shared-user"

    def timedOut(self):
        return time.time() > self.waitUntil

    def invalid(self):
        """
        indicates the captcha was not correct.
        """
        [x.captchaInvalid(self) for x in self.handler]

    def correct(self):
        [x.captchaCorrect(self) for x in self.handler]

    def __str__(self):
        return f"<CaptchaTask '{self.id}'>"
