# Copyright 2013 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import ConfigParser
import functools
import os
import tempfile
import re
import selinux
import io


(
    BEFORE,
    WITHIN,
    AFTER
) = range(3)


def context(func):
    @functools.wraps(func)
    def inner(*args, **kwargs):
        if not args[0].context:
            raise RuntimeError("Must be called from a managed context.")
        func(*args, **kwargs)
    return inner


class ConfigFile(object):

    def __init__(self, filename, sectionStart, sectionEnd, version):
        """
        configuration versioning:
        When adding a section or a list of entries they will be
        prepended and appended;
        'sectionStart'-'version'\n
        ...
        'sectionEnd'-'version'\n.

        When removing/checking existing we search for the section between lines
        starting with 'sectionStart' and 'sectionEnd'. so we read and remove
        previous versions and add a new one.
        """
        if not os.path.exists(filename):
            raise OSError(
                'No such file or directory: %s' % (filename, )
            )

        self.filename = filename
        self.context = False
        self.sectionStart = sectionStart
        self.sectionEnd = sectionEnd
        self.version = version

    def __enter__(self):
        if self.context:
            raise RuntimeError("can only enter once")
        self.entries = {}
        self.context = True
        self.prefix = None
        self.section = None
        self.oldmod = os.stat(self.filename).st_mode
        self.remove = None
        self.rmstate = BEFORE
        return self

    def getOldContent(self):
        confpat = re.compile(r'^\s*(?P<key>[^=\s#]*)\s*=')
        oldlines = []
        oldentries = set()
        with open(self.filename, 'r') as f:
            for line in f:
                if self.remove:
                    if (self.rmstate == BEFORE and
                            line.startswith(self.sectionStart)):
                            self.rmstate = WITHIN
                    elif self.rmstate == WITHIN and\
                            line.startswith(self.sectionEnd):
                            self.rmstate = AFTER
                            continue
                if not self.remove or self.rmstate != WITHIN:
                    m = confpat.match(line.rstrip())
                    if m:
                        oldentries.add(m.group('key'))
                    if self.prefix:
                        line = self.prefix + line
                    oldlines.append(line)
            return oldlines, oldentries

    def _start(self):
        return "%s-%s\n" % (self.sectionStart, self.version)

    def _end(self):
        return "%s-%s\n" % (self.sectionEnd, self.version)

    def _writeSection(self, f):
        f.write(self._start())
        f.write(self.section)
        f.write(self._end())

    def _writeEntries(self, f, oldentries):
        f.write(self._start())
        for key, val in self.entries.iteritems():
            if key not in oldentries:
                f.write("{k}={v}\n".format(k=key, v=val))
        f.write(self._end())

    def __exit__(self, exec_ty, exec_val, tb):

        self.context = False
        if exec_ty is None:
            fd, tname = tempfile.mkstemp(dir=os.path.dirname(self.filename))
            try:
                oldlines, oldentries = self.getOldContent()
                with os.fdopen(fd, 'w', ) as f:
                    if self.section:
                        self._writeSection(f)
                    f.writelines(oldlines)
                    if self.entries:
                        self._writeEntries(f, oldentries)
                os.rename(tname, self.filename)
                if self.oldmod != os.stat(self.filename).st_mode:
                    os.chmod(self.filename, self.oldmod)

                if selinux.is_selinux_enabled:
                    try:
                        selinux.restorecon(self.filename)
                    except OSError:
                        pass  # No default label for file
            finally:
                if os.path.exists(tname):
                    os.remove(tname)

    @context
    def addEntry(self, key, val):
        """
        add key=value unless key is already in the file.
        """
        self.entries[key] = val

    @context
    def prependSection(self, section):
        """
        add 'section' in the beginning of the file.
        section is wrapped with sectionStart and sectionEnd.
        Only one section is currently allowed.
        """
        self.section = section

    @context
    def prefixLines(self, prefix):
        """
        prefix each line originaly included in the file (not including
         'prependedSection' lines) with 'prefix'.
        """
        self.prefix = prefix

    @context
    def removeConf(self):
        self.remove = True

    def hasConf(self):
        """
        Notice this method is called out of context since it is read only
        """
        for line in open(self.filename, 'r'):
            if line == self._start():
                return True
        return False


class ParserWrapper(object):
    """
    ConfigParser is for parsing of ini files. Use this
    class for files with no sections.
    """
    def __init__(self, defaults=None):
        self.wrapped = ConfigParser.RawConfigParser(defaults=defaults)

    def get(self, option):
        return self.wrapped.get('root', option)

    def getboolean(self, option):
        return self.wrapped.getboolean('root', option)

    def getfloat(self, option):
        return self.wrapped.getfloat('root', option)

    def getint(self, option):
        return self.wrapped.getint('root', option)

    def read(self, path):
        with open(path, 'r') as f:
            return self.wrapped.readfp(
                io.StringIO(u'[root]\n' + f.read().decode())
            )