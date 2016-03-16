from twisted.internet.defer import gatherResults
from twisted.web.client import getPage

from rhumba import RhumbaPlugin, cron


class Plugin(RhumbaPlugin):
    """
    A plugin to periodically push an application group definition to Marathon.
    """

    getPage = getPage  # To stub out in tests.

    def __init__(self, *args, **kw):
        super(Plugin, self).__init__(*args, **kw)

        self.marathon_host = self.config.get("marathon_host", "localhost")
        self.marathon_port = self.config.get("marathon_port", "8080")
        self.group_json_files = self.config["group_json_files"]

    @cron(min="*/1")
    def call_do_thing(self, args):
        self.log("Did thing: %r" % (args,))
        return self.call_update_groups()

    def call_update_groups(self):
        ds = []
        for group_json_file in self.group_json_files:
            ds.append(self.call_update_group(group_json_file))
        return gatherResults(ds)

    def call_update_group(self, group_json_file):
        self.log("Updating %r" % (group_json_file,))
        body = self.readfile(group_json_file)
        d = self._call_marathon("PUT", "v2/groups", body)
        d.addBoth(self._logcb)
        return d

    def _logcb(self, r, msg=" ... result: %r"):
        self.log(msg % (r,))
        return r

    def _call_marathon(self, method, path, body=None):
        uri = b"http://%s:%s/%s" % (
            self.marathon_host, self.marathon_port, path)
        return self.getPage(uri, method=method, postdata=body)

    def readfile(self, filepath):
        """
        Read a file and return its content.
        """
        with open(filepath, "r") as f:
            return f.read()
