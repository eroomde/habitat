# Copyright 2010 (C) Daniel Richman
#
# This file is part of habitat.
#
# habitat is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# habitat is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with habitat.  If not, see <http://www.gnu.org/licenses/>.

"""
The code in this module drives the "main" method

``bin/habitat`` simply does the following::

    import habitat
    habitat.main.Program().main()
"""

import sys
import os
import signal
import threading
import Queue
import errno
import logging
import optparse
import ConfigParser

import habitat
from habitat.message_server import Server
from habitat.http import SCGIApplication

__all__ = ["get_options", "setup_logging", "Program", "SignalListener"]

usage = "%prog [options]"
version = "{0} {1}".format(habitat.__name__, habitat.__version__)
header = "{0} is {1}".format(habitat.__name__, habitat.__copyright__)

default_configuration_file = "/etc/habitat/habitat.cfg"
"""The default location to search for a configuration file"""

config_section = "habitat"
"""The section in the config file to search for options"""

parser = optparse.OptionParser(usage=usage, version=version,
                               description=header)
parser.add_option("-f", "--config-file", metavar="CONFIG_FILE",
                  dest="config_file",
                  help="file from which other options may be read")
parser.add_option("-c", "--couch", metavar="COUCH_SERVER",
                  dest="couch",
                  help="couch server to connect to")
parser.add_option("-s", "--socket", metavar="SCGI_SOCKET",
                  dest="socket_file",
                  help="scgi socket file to serve on")
parser.add_option("-v", "--verbosity", metavar="LOG_STDERR_LEVEL",
                  dest="log_stderr_level",
                  help="minimum loglevel to print on stderr, options: " +\
                       "NONE, DEBUG, INFO, WARN, ERROR, CRITICAL")
parser.add_option("-l", "--log-file", metavar="LOG_FILE",
                  dest="log_file",
                  help="file name to send log messages to")
parser.add_option("-e", "--log-level", metavar="LOG_FILE_LEVEL",
                  dest="log_file_level",
                  help="minimum loglevel to log to file " + \
                       "(see verbosity for options)")

def get_options():
    """
    **get_options** reads command line options and a configuration file

    This function parses command line options, and reads a
    configuration file (which must be in the :py:mod:`ConfigParser`
    format).

    It will read :py:data:`default_configuration_file` and will ignore
    any errors that occur while doing so, unless a different config
    file is specified at the command line (failures on an explicitly
    stated config file will raise an execption).

    Command line options have priority over options from a config file.
    """

    (option_values, args) = parser.parse_args()

    if len(args) != 0:
        parser.error("did not expect any positional arguments")

    # A dict is arguably easier to use.
    options = option_values.__dict__.copy()

    # I would use optparse.set_defaults but we need to know whether
    # the option was explicitly stated
    if options["config_file"] == None:
        config_file = default_configuration_file
        config_file_explicit = False
    else:
        config_file = options["config_file"]
        config_file_explicit = True
    del options["config_file"]

    config = ConfigParser.RawConfigParser()
    try:
        with open(config_file, "r") as f:
            config.readfp(f, config_file)
    except IOError, e:
        # If the error was in opening the default config file - not explicitly
        # set - then ignore it.
        if config_file_explicit:
            parser.error("error opening {0}: {1}".format(config_file, e))
    except ConfigParser.ParsingError, e:
        parser.error("error parsing {0}: {1}".format(config_file, e))
    else:
        config_items = dict(config.items(config_section))
        for dest in options.keys():
            if options[dest] == None and config_items.has_key(dest):
                options[dest] = config_items[dest]

    required_options = ["couch", "socket_file"]

    if options["log_file"] != None or options["log_file_level"] != None:
        required_options += ["log_file", "log_file_level"]

    for dest in required_options:
        if options[dest] == None or options[dest] == "":
            parser.error("\"{0}\" was not specified".format(dest))

    # I would use the "choice" type for parser and have it validate
    # these options, but we also need to check options provided in a
    # config file, and add a "NONE" option
    LOG_LEVELS = ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"]
    NONE_LEVELS = ["NONE", "SILENT", "QUIET"]

    for levelarg in ["log_stderr_level", "log_file_level"]:
        lvl = options[levelarg]
        if lvl == None:
            continue
        lvl = lvl.upper()

        if lvl in NONE_LEVELS:
            options[levelarg] = None
        elif lvl not in LOG_LEVELS:
            parser.error("invalid value for \"{0}\"".format(levelarg))
        else:
            options[levelarg] = getattr(logging, lvl)

    return options

def setup_logging(log_stderr_level, log_file_name, log_file_level):
    """
    **setup_logging** initalises the :py:mod:`Python logging module <logging>`.

    It will initalise the 'habitat' logger and creates one, two, or no
    Handlers, depending on the values provided for *log_file_level* and
    *log_stderr_level*.
    """

    formatstring = "[%(asctime)s] %(levelname)s %(name)s %(threadName)s: " + \
                   "%(message)s"

    root_logger = logging.getLogger()

    # Enable all messages at the logger level, then filter them in each
    # handler.
    root_logger.setLevel(logging.DEBUG)

    have_handlers = False

    if log_stderr_level != None:
        stderr_handler = logging.StreamHandler()
        stderr_handler.setFormatter(logging.Formatter(formatstring))
        stderr_handler.setLevel(log_stderr_level)
        root_logger.addHandler(stderr_handler)
        have_handlers = True

    if log_file_level != None:
        file_handler = logging.FileHandler(log_file_name)
        file_handler.setFormatter(logging.Formatter(formatstring))
        file_handler.setLevel(log_file_level)
        root_logger.addHandler(file_handler)
        have_handlers = True

    if not have_handlers:
        # logging gets annoyed if there isn't atleast one handler.
        # If we're meant to be totally silent...
        root_logger.addHandler(logging.NullHandler())

class Program:
    """
    Program provides the :py:meth:`main`, :py:meth:`shutdown`, \
    :py:meth:`reload` and :py:meth:`panic` methods
    """

    (RELOAD, SHUTDOWN) = range(2)

    def __init__(self):
        self.queue = Queue.Queue()

    def main(self):
        """
        The main method of habitat

        This method does the following:

         - calls :py:func:`get_options`
         - creates a :py:class:`habitat.message_server.Server`
         - creates a :py:class:`habitat.http.SCGIApplication`
         - creates a :py:class:`SignalListener`
         - starts the SCGI app thread
         - starts the Program thread (see :py:meth:`Program.run`)
         - starts the SignalListner thread

        """

        # Setup phase: before any threads are started.
        self.options = get_options()
        setup_logging(self.options["log_stderr_level"],
                      self.options["log_file"],
                      self.options["log_file_level"])
        self.server = Server(None, self)
        self.scgiapp = SCGIApplication(self.server, self,
                                       self.options["socket_file"])
        self.signallistener = SignalListener(self)
        self.thread = threading.Thread(target=self.run,
                                       name="Shutdown Handling Thread")

        self.signallistener.setup()
        self.scgiapp.start()
        self.thread.start()

        self.signallistener.listen()

    def reload(self):
        """asks the Program thread to process a **RELOAD** event"""
        self.queue.put(Program.RELOAD)

    def shutdown(self):
        """asks the Program thread to process a **SHUTDOWN** event"""
        self.queue.put(Program.SHUTDOWN)

    def panic(self):
        """
        calls :py:func:`signal.alarm` as a failsafe and then \
        :py:meth:`shutdown`
        """

        signal.alarm(60)
        self.shutdown()

    def run(self):
        """
        The Program thread processes **SHUTDOWN** and **RELOAD** events

        In order to make :py:meth:`shutdown`, :py:meth:`reload` and
        :py:meth:`panic` return instantly, the actual work requested
        by calling those methods is done by this thread.

         - **RELOAD**: To be implemented
         - **SHUTDOWN**: shuts down the :py:class:`SignalListener`,
           :py:class:`habitat.http.SCGIApplication` and the
           :py:class:`habitat.message_server.Server`, then calls
           :py:func:`sys.exit`. Having shut down the above three,
           this should be the only thread executing, so the process
           will exit.

        """

        while True:
            item = self.queue.get()

            if item == Program.SHUTDOWN:
                self.signallistener.exit()
                self.scgiapp.shutdown()
                self.server.shutdown()
                sys.exit()
            elif item == Program.RELOAD:
                # TODO
                pass

class SignalListener:
    """
    This class listens for signals

    It responds to the following signals. When it receives one, it
    calls the appropriate method of Program

    The documentation for the :py:mod:`signal` module contains
    information on the various signal constant definitions.

     - **SIGTERM**, **SIGINT**: calls :py:meth:`Program.shutdown`
     - **SIGHUP**: calls :py:meth:`Program.reload`
     - **SIGUSR1**: exits the :py:meth:`listen` loop by
       calling :py:func:`sys.exit` / raising
       :py:exc:`SystemExit <exceptions.SystemExit>`
       (NB: the :py:meth:`listen` loop will be running in **MainThread**)

    **SIGUSR1** is meant for internal use only, and is
    used to terminate the signal-listening thread when the program wishes
    to shut down.
    (see :py:meth:`SignalListener.exit`)
    """

    def __init__(self, program):
        self.program = program
        self.shutdown_event = threading.Event()

    def check_thread(self):
        assert threading.current_thread().name == "MainThread"

    def setup(self):
        """
        **setup()** installs signal handlers for the signals that we want

        Must be called in the **MainThread**
        """

        self.check_thread()

        for signum in [signal.SIGTERM, signal.SIGINT,
                       signal.SIGHUP, signal.SIGUSR1]:
            signal.signal(signum, self.handle)

    def listen(self):
        """
        **listen()** listens for signals delivered to the process forever

        It calls :py:func:`signal.pause` indefinitely, meaning that
        any signal sent to the process can be caught instantly and
        unobtrusivly.

        Must be called in the **MainThread**
        """

        self.check_thread()

        try:
            while True:
                signal.pause()
        except SystemExit:
            self.shutdown_event.set()
            raise

    def exit(self):
        """
        **exit()** terminates the :py:meth:`listen` loop

        It raises **SIGUSR1** in this process, causing
        the infinite listen() loop to exit
        (:py:meth:`SignalListener.handle` will call
        :py:func:`sys.exit`)
        """

        os.kill(os.getpid(), signal.SIGUSR1)
        self.shutdown_event.wait()

    def handle(self, signum, stack):
        """handles a received signal"""

        if signum == signal.SIGTERM or signum == signal.SIGINT:
            self.program.shutdown()
        elif signum == signal.SIGHUP:
            self.program.reload()
        elif signum == signal.SIGUSR1:
            sys.exit()
