# -*- coding: utf-8 -*-
# AUTHOR: RaNaN

import os
import time

from re import search

import pycurl

from .http_request import HTTPRequest


class WrongFormat(Exception):
    pass


class ChunkInfo:
    def __init__(self, name):
        self.name = str(name)
        self.size = 0
        self.resume = False
        self.chunks = []

    def __repr__(self):
        ret = f"ChunkInfo: {self.name}, {self.size}\n"
        for i, c in enumerate(self.chunks):
            ret += f"{i}# {c[1]}\n"

        return ret

    def setSize(self, size):
        self.size = int(size)

    def addChunk(self, name, range):
        self.chunks.append((name, range))

    def clear(self):
        self.chunks = []

    def createChunks(self, chunks):
        self.clear()
        chunk_size = self.size // chunks

        current = 0
        for i in range(chunks):
            end = self.size - 1 if (i == chunks - 1) else current + chunk_size
            self.addChunk(f"{self.name}.chunk{i}", (current, end))
            current += chunk_size + 1

    def save(self):
        fs_name = os.fsdecode(f"{self.name}.chunks")
        with open(fs_name, mode="w", encoding="utf-8") as fh:
            fh.write(f"name:{self.name}\n")
            fh.write(f"size:{self.size}\n")
            for i, c in enumerate(self.chunks):
                fh.write(f"#{i}:\n")
                fh.write(f"\tname:{c[0]}\n")
                fh.write(f"\trange:{c[1][0]}-{c[1][1]}\n")

    @staticmethod
    def load(name):
        fs_name = os.fsdecode(f"{name}.chunks")
        if not os.path.exists(fs_name):
            raise IOError
        with open(fs_name, encoding="utf-8") as fh:
            name = fh.readline()[:-1]
            size = fh.readline()[:-1]
            if name.startswith("name:") and size.startswith("size:"):
                name = name[5:]
                size = size[5:]
            else:
                fh.close()
                raise WrongFormat
            ci = ChunkInfo(name)
            ci.loaded = True
            ci.setSize(size)
            while True:
                if not fh.readline():  #: skip line
                    break
                name = fh.readline()[1:-1]
                range = fh.readline()[1:-1]
                if name.startswith("name:") and range.startswith("range:"):
                    name = name[5:]
                    range = range[6:].split("-")
                else:
                    raise WrongFormat

                ci.addChunk(name, (int(range[0]), int(range[1])))

        return ci

    def remove(self):
        fs_name = os.fsdecode(f"{self.name}.chunks")
        if os.path.exists(fs_name):
            os.remove(fs_name)

    def getCount(self):
        return len(self.chunks)

    def getChunkName(self, index):
        return self.chunks[index][0]

    def getChunkRange(self, index):
        return self.chunks[index][1]


class HTTPChunk(HTTPRequest):
    def __init__(self, id, parent, range=None, resume=False):
        self.id = id
        self.p = parent  #: HTTPDownload instance
        self.range = range  #: tuple (start, end)
        self.resume = resume
        self.log = parent.log

        self.size = range[1] - range[0] if range else -1
        self.arrived = 0
        self.lastURL = self.p.referer

        self.c = pycurl.Curl()

        self.header = ""
        self.headerParsed = False  #: indicates if the header has been processed

        self.fp = None  #: file handle

        self.initHandle()
        self.setInterface(self.p.options)

        self.BOMChecked = False  #: check and os.remove byte order mark

        self.rep = None

        self.sleep = 0.000
        self.lastSize = 0

    def __repr__(self):
        return f"<HTTPChunk id={self.id}, size={self.size}, arrived={self.arrived}>"

    @property
    def cj(self):
        return self.p.cj

    def getHandle(self):
        """
        returns a Curl handle ready to use for perform/multiperform.
        """
        self.setRequestContext(
            self.p.url, self.p.get, self.p.post, self.p.referer, self.p.cj
        )
        self.c.setopt(pycurl.WRITEFUNCTION, self.writeBody)
        self.c.setopt(pycurl.HEADERFUNCTION, self.writeHeader)

        # request all bytes, since some servers in russia seems to have a defect
        # arihmetic unit

        fs_name = os.fsdecode(self.p.info.getChunkName(self.id))
        if self.resume:
            self.fp = open(fs_name, mode="ab")
            self.arrived = self.fp.tell()
            if not self.arrived:
                self.arrived = os.stat(fs_name).st_size

            if self.range:
                # do nothing if chunk already finished
                if self.arrived + self.range[0] >= self.range[1]:
                    return None

                start = self.arrived + self.range[0]
                if (
                    self.id == len(self.p.info.chunks) - 1
                ):  #: as last chunk dont set end range, so we get everything
                    end = ""
                else:
                    end = min(self.range[1] + 1, self.p.size - 1)

                range = f"{start}-{end}"

                self.log.debug(f"Chunked resume with range {range}")
                self.c.setopt(pycurl.RANGE, range)
            else:
                self.log.debug(f"Resume File from {self.arrived}")
                self.c.setopt(pycurl.RESUME_FROM, self.arrived)

        else:
            if self.range:
                start = self.range[0]
                if self.id == len(self.p.info.chunks) - 1:  #: see above
                    end = ""
                else:
                    end = min(self.range[1] + 1, self.p.size - 1)

                range = f"{start}-{end}"

                self.log.debug(f"Chunked with range {range}")
                self.c.setopt(pycurl.RANGE, range)

            self.fp = open(fs_name, mode="wb")

        return self.c

    def writeHeader(self, buf):
        self.header += buf
        # TODO: forward headers?, this is possibly unneeeded, when we just parse valid 200 headers
        # as first chunk, we will parse the headers
        if not self.range and self.header.endswith("\r\n\r\n"):
            self.parseHeader()
        # ftp file size parsing
        elif not self.range and buf.startswith("150") and "data connection" in buf:
            size = search(r"(\d+) bytes", buf)
            if size:
                self.p.size = int(size.group(1))
                self.p.chunkSupport = True

        self.headerParsed = True

    def writeBody(self, buf):
        # ignore BOM, it confuses unrar
        if not self.BOMChecked:
            if [ord(b) for b in buf[:3]] == [239, 187, 191]:
                buf = buf[3:]
            self.BOMChecked = True

        size = len(buf)

        self.arrived += size

        self.fp.write(buf)

        if self.p.bucket:
            time.sleep(self.p.bucket.consumed(size))
        else:
            # Avoid small buffers, increasing sleep time slowly if buffer size gets smaller
            # otherwise reduce sleep time percentual (values are based on tests)
            # So in general cpu time is saved without reducing bandwith too much

            if size < self.lastSize:
                self.sleep += 0.002
            else:
                self.sleep *= 0.7

            self.lastSize = size

            time.sleep(self.sleep)

        if self.range and self.arrived > self.size:
            return 0  #: close if we have enough data

    def parseHeader(self):
        """
        parse data from recieved header.
        """
        for orgline in self.decodeResponse(self.header).splitlines():
            line = orgline.strip().lower()
            if line.startswith("accept-ranges") and "bytes" in line:
                self.p.chunkSupport = True

            if line.startswith("content-disposition") and "filename=" in line:
                name = orgline.partition("filename=")[2]
                name = name.replace('"', "").replace("'", "").replace(";", "").strip()
                self.p.nameDisposition = name
                self.log.debug(f"Content-Disposition: {name}")

            if not self.resume and line.startswith("content-length"):
                self.p.size = int(line.split(":")[1])

        self.headerParsed = True

    def stop(self):
        """
        The download will not proceed after next call of writeBody.
        """
        self.range = [0, 0]
        self.size = 0

    def resetRange(self):
        """
        Reset the range, so the download will load all data available.
        """
        self.range = None

    def setRange(self, range):
        self.range = range
        self.size = range[1] - range[0]

    def flushFile(self):
        """
        flush and close file.
        """
        self.fp.flush()
        os.fsync(self.fp.fileno())  #: make sure everything was written to disk
        self.fp.close()  #: needs to be closed, or merging chunks will fail

    def close(self):
        """
        closes everything, unusable after this.
        """
        if self.fp:
            self.fp.close()
        self.c.close()
        if hasattr(self, "p"):
            del self.p
