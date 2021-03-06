#!/usr/bin/env python

import sys
import re
import datetime
import fileinput

if sys.platform == 'win32':
    ## on windows, we need to use the following reactor for serial support
    ## http://twistedmatrix.com/trac/ticket/3802
    from twisted.internet import win32eventreactor
    win32eventreactor.install()

from twisted.internet import reactor
print "Using Twisted reactor", reactor.__class__
print

from zope.interface import implements

from twisted.python import usage, log
from twisted.protocols.basic import LineReceiver
from twisted.internet.serialport import SerialPort
from twisted.internet.task import LoopingCall
from twisted.internet.ssl import DefaultOpenSSLContextFactory
from twisted.web.server import Site
from twisted.web.static import File
from twisted.web.guard import HTTPAuthSessionWrapper, DigestCredentialFactory
from twisted.web.resource import IResource
from twisted.cred.portal import IRealm, Portal
from twisted.cred.checkers import FilePasswordDB

from redirectscheme import RedirectToScheme

from autobahn.websocket import listenWS, createWsUrl
from autobahn.wamp import WampServerFactory, WampServerProtocol, exportRpc


class Serial2WsOptions(usage.Options):
    optParameters = [
      ['baudPower', '', 9600, 'Serial baudrate for Power settings'],
      ['baudRDS', '', 19200, 'Serial baudrate for RDS settings'],
      ['port', 'p', '/dev/ttyUSB0', 'Serial port to use'],
      ['webport', 'w', 80, 'Web port to use for embedded Web server'],
      ['securewebport', '', 443, 'Secure Web port to use for embedded Web server'],
      ['websocketport', 's', "9000",\
                    'WebSocket port to use for embedded WebSocket server']
    ]


## MCU protocol
## knows nothing about websockets
class McuProtocol(LineReceiver):
    delimiter = '$'

    ## need a reference to our WS-MCU gateway factory to dispatch PubSub events
    def __init__(self, wsMcuFactory, baudPower, baudRDS):
        self.wsMcuFactory = wsMcuFactory
        self.baudPower    = baudPower
        self.baudRDS      = baudRDS

    ## this method is exported as RPC and can be called by connected clients
    @exportRpc("read-tx")
    def readTX(self):
        self.writeTransmitter("FM", '\x20', self.baudPower)

    @exportRpc("set-power")
    def setPower(self, power):
        if 0 <= float(power) <= 100.0:
            power_s = chr(int(round(int(power) / 100.0 * 34) + 4))
            self.writeTransmitter("FO", power_s, self.baudPower)

    @exportRpc("set-freq")
    def setFrequency(self, freq):
        if 87.5 <= float(freq) <= 108.0:
            # convert from ASCII and from Mhz to KHz
            freq = float(freq) * 1000
            #low part of freq
            low = chr((int(freq / 5) - int(int(freq / 5) / 128) * 128) + 4)
            #high part of freq
            high = chr((int(int(freq / 5) / 128)) + 4)
            self.writeTransmitter("FF", low + high, self.baudPower)

    @exportRpc("set-channels")
    def setChannels(self, channels):
        if 0 <= int(channels) <= 1:
            # convert from int to ASCII
            channels = str(channels)
            self.writeTransmitter("FS", channels, self.baudPower)

    @exportRpc("set-DSP")
    def setDSP(self, attack, decay, interval, threshold, compression):
        self.setAttack(attack)
        self.setDecay(decay)
        self.setIntegration(interval)
        self.setThreshold(threshold)
        self.setCompression(compression)

    @exportRpc("set-alarms")
    def setAlarms(self, swr_alarm, current_alarm, temp_alarm, Uamp_alarm):
        self.setTempAlarm(temp_alarm)
        self.setSWRAlarm(swr_alarm)
        self.setUampAlarm(Uamp_alarm)
        self.setIampAlarm(current_alarm)

    @exportRpc("set-bass-treble")
    def setBassTreble(self, treble, bass):
        self.setTreble(treble)
        self.setBass(bass)

    @exportRpc("set-audio")
    def setAudio(self, left_gain, right_gain):
        self.setRightGain(right_gain)
        self.setLeftGain(left_gain)

    def setTreble(self, treble):
        self.writeTransmitter("FDT", chr(int(treble) + 4), self.baudPower)

    def setBass(self, bass):
        self.writeTransmitter("FDB", chr(int(bass) + 4), self.baudPower)

    def setAttack(self, attack):
        self.writeTransmitter("FDA", chr(int(attack) + 4), self.baudPower)

    def setDecay(self, decay):
        self.writeTransmitter("FDD", chr(int(decay) + 4), self.baudPower)

    def setThreshold(self, threshold):
        self.writeTransmitter("FDH", chr(int(threshold) + 4), self.baudPower)

    def setCompression(self, compression):
        self.writeTransmitter("FDC", chr(int(compression) + 4), self.baudPower)

    def setIntegration(self, integration):
        self.writeTransmitter("FDI", chr(int(integration) + 4), self.baudPower)

    def setLeftGain(self, leftGain):
        self.writeTransmitter("FDGL", chr(int(leftGain) + 4), self.baudPower)

    def setRightGain(self, rightGain):
        self.writeTransmitter("FDGR", chr(int(rightGain) + 4), self.baudPower)

    def setTempAlarm(self, tempAlarm):
        self.writeTransmitter("FAT", chr(int(tempAlarm) + 4), self.baudPower)

    def setSWRAlarm(self, SWRAlarm):
        self.writeTransmitter("FAS", chr(int(SWRAlarm) + 4), self.baudPower)

    def setUampAlarm(self, UampAlarm):
        self.writeTransmitter("FAU", chr(int(UampAlarm) + 4), self.baudPower)

    def setIampAlarm(self, IampAlarm):
        self.writeTransmitter("FAC", chr(int(IampAlarm) + 4), self.baudPower)

    # RDS COMMANDS
    @exportRpc("set-RDS")
    def setRDS(self, state):
        self.writeTransmitter("PWR", state.encode('ascii', 'ignore'), self.baudRDS)

    @exportRpc("PI-code")
    def setPICode(self, country, country_ecc, area_coverage, pr):
        if  0 <= int(pr) <= 255:
            self.writeTransmitter("CCAC", str(int(country) * 16 + int(area_coverage)), self.baudRDS)
            self.writeTransmitter("ECC", chr(int(country_ecc) + 4), self.baudRDS)
            self.writeTransmitter("PREF", str(int(pr)), self.baudRDS)

    @exportRpc("A0-settings")
    def setA0Settings(self, tp, ta, ms, dyn_pty, compression, channels, ah, program_type):
        self.writeTransmitter("TP",tp.encode('ascii', 'ignore'), self.baudRDS)
        self.writeTransmitter("TA",ta.encode('ascii', 'ignore'), self.baudRDS)
        self.writeTransmitter("MS",ms.encode('ascii', 'ignore'), self.baudRDS)
        self.writeTransmitter("Did3",dyn_pty.encode('ascii', 'ignore'), self.baudRDS)
        self.writeTransmitter("Did2",compression.encode('ascii', 'ignore'), self.baudRDS)
        self.writeTransmitter("Did0",channels.encode('ascii', 'ignore'), self.baudRDS)
        self.writeTransmitter("Did1",ah.encode('ascii', 'ignore'), self.baudRDS)
        self.writeTransmitter("PTY",program_type.encode('ascii', 'ignore'), self.baudRDS)

    @exportRpc("PF-alternative")
    # arrays in javascript are received as lists
    def setPFAlternative(self, cb, freqs):
          AFNum = 0
          for c in cb:
              if c:
                  AFNum+=1
              else:
                  break
          self.writeTransmitter("AF0", chr(AFNum + 224 + 4), self.baudRDS)

          for k in range(0,AFNum):
              freq = float(freqs[k])
              # NOTE: these limits seem weird,
              # but are the ones used in the windows program
              if 87.6 <= freq <= 107.9:
                  freq_index = int((freq*1000 - 87500) / 100.0)
                  self.writeTransmitter("AF" + str(k+1), chr(freq_index + 4), self.baudRDS)

    @exportRpc("static-PS")
    def setStaticPS(self, msg):
        if  0 < len(msg) <= 8:
            # translate from unicode, and add spaces up to 8 chars
            msg2 = msg.encode('ascii', 'ignore')
            msg2 = '%-8s' % msg2
            self.writeTransmitter("PS00", msg2, self.baudRDS)
            # set constant delay of 9 minutes
            self.writeTransmitter("PD00", "9", self.baudRDS)

            # set all other PS to empty spaces and 0 minutes delay
            empty = "        "
            for k in range(1,100):
                self.writeTransmitter("PS" + "%02d" %k, empty, self.baudRDS)
                self.writeTransmitter("PD" + "%02d" %k, "0", self.baudRDS)

    @exportRpc("sync-time")
    def setSyncTime(self):
        t = datetime.datetime.now()
        self.writeTransmitter("TIME", chr(t.hour + 4) + chr(t.minute + 4) + chr(t.second + 4), self.baudRDS)
        self.writeTransmitter("DATE", chr(t.year - 2004 + 4) + chr(t.month + 4) + chr(t.day + 4), self.baudRDS)

    @exportRpc("RT")
    def setExternalMessages(self, msg):
        if  0 < len(msg) <= 64:
            msg2 = msg.encode('ascii', 'ignore')
            msg2 = '%-64s' % msg2
            self.writeTransmitter("RT", msg2, self.baudRDS)

    def writeTransmitter(self, command, data, baudRate):
        try:
            currentBaudRate = self.transport._serial.getBaudrate()
            if currentBaudRate != baudRate:
                self.transport._serial.setBaudrate(baudRate)
            self.transport.write('\x00\x00' + command + '\x01' + data + '\x02')
        except TypeError:
            print "Wrong type data"

    def connectionMade(self):
        log.msg('Serial port connected.')

    def lineReceived(self, line):
        try:
            AlarmCode = int(re.search('AlarmCode=(.+?),', line).group(1))
            Pfwd = float(re.search('Pfwd=(.+?)W,', line).group(1))
            ref = float(re.search('ref=(.+?)W,', line).group(1))
            Texc = float(re.search('Texc=(.+?)C,', line).group(1))
            Tamp = float(re.search('Tamp=(.+?)C,', line).group(1))
            Uexc = float(re.search('Uexc=(.+?)V,', line).group(1))
            Uamp = float(re.search('Uamp=(.+?)V,', line).group(1))
            Audio = ord(re.search('Audio=(.+?),', line).group(1))
            Iexc = float(re.search('Iexc=(.+?)A,', line).group(1))
            Iamp = float(re.search('Iamp=(.+?)A,', line).group(1))
            Uptime = re.search('Uptime=(.+?),', line).group(1)
            Freq = float(re.search('Freq=(.+?)MHz,', line).group(1))
            Firmware = re.search('Firmware=(.+?),', line).group(1)
            PowerLimit = re.search('PowerLimit=(.+?),', line).group(1)
            evt = {'AlarmCode': AlarmCode, 'Pfwd': Pfwd, 'ref': ref,\
                'Texc': Texc, 'Tamp': Tamp, 'Uexc': Uexc, 'Uamp': Uamp,\
                'Audio': Audio, 'Iexc': Iexc, 'Iamp': Iamp, 'Uptime': Uptime,\
                'Freq': Freq, 'Firmware': Firmware, 'PowerLimit': PowerLimit}
            self.wsMcuFactory.dispatch("http://example.com/mcu#tx-status", evt)
        except AttributeError:
            print "Could not parse string."

    def updateDatetime(self):
        date = str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.wsMcuFactory.dispatch("http://example.com/mcu#update-time", date)


## WS-MCU protocol
## knows about websockets
## registers for RPC and PubSub
class WsMcuProtocol(WampServerProtocol):

    def onSessionOpen(self):
        ## register topic prefix under which we will publish MCU measurements
        self.registerForPubSub("http://example.com/mcu#", True)

        ## register methods for RPC
        self.registerForRpc(self.factory.mcuProtocol,
                                    "http://example.com/mcu-control#")


## WS-MCU factory
## knows about websockets
class WsMcuFactory(WampServerFactory):

    protocol = WsMcuProtocol

    def __init__(self, url, baudPower, baudRDS):
        WampServerFactory.__init__(self, url)
        self.mcuProtocol = McuProtocol(self, baudPower, baudRDS)


## Used to replace in "transmitter.js" the choosen port for websocket.
## Maybe a better could be used.
def replaceWebSocketPort(wsport):
    for line in fileinput.input("transmitter.js", inplace = 1):
        if line.startswith('var wsuri = "wss://" + window.location.hostname'):
            print 'var wsuri = "wss://" + window.location.hostname + ":' + str(wsport) +'";'
        else:
            print line,

## Realm needed for Authentication
class SimpleRealm(object):
    implements(IRealm)

    def __init__(self, Resource):
        self.resource = Resource

    def requestAvatar(self, avatarId, mind, *interfaces):
        if IResource in interfaces:
            return IResource, self.resource, lambda: None
        raise NotImplementedError()


if __name__ == '__main__':

    ## parse options
    o = Serial2WsOptions()
    try:
        o.parseOptions()
    except usage.UsageError, errortext:
        print '%s %s' % (sys.argv[0], errortext)
        print 'Try %s --help for usage details' % sys.argv[0]
        sys.exit(1)

    baudPower = int(o.opts['baudPower'])
    baudRDS = int(o.opts['baudRDS'])
    port = o.opts['port']
    webport = int(o.opts['webport'])
    securewebport = int(o.opts['securewebport'])
    wsport = int(o.opts['websocketport'])

    ## start Twisted log system
    log.startLogging(sys.stdout)

    ## Replace websocket port
    replaceWebSocketPort(wsport)

    ## Create a secure websocket url
    wsurl = createWsUrl("localhost", wsport, True)

    ## create Serial2Ws gateway factory
    wsMcuFactory = WsMcuFactory(wsurl, baudPower, baudRDS)
    ## calls reactor internally
    listenWS(wsMcuFactory, DefaultOpenSSLContextFactory(
                                      'keys/server.key', 'keys/server.crt'))

    ## create serial port and serial port protocol
    log.msg('About to open serial port %s [%d baud] ..' % (port, baudPower))
    serialPort = SerialPort(wsMcuFactory.mcuProtocol, port,\
                           reactor, baudrate=baudPower)

    ## Looping Call to keep updating date and time on client
    lc = LoopingCall(wsMcuFactory.mcuProtocol.updateDatetime)
    lc.start(1)

    ## Redirect normal http to https
    reactor.listenTCP(webport, Site(RedirectToScheme("https", securewebport)))

    ## create embedded web server for static files
    webdir = File(".")   # Resource
    web = Site(webdir)

    ## necessary stuff for authentication
    checkers = [FilePasswordDB('httpd.password')]
    portal = Portal(SimpleRealm(web), checkers)
    credentialFactory = DigestCredentialFactory("md5", "localhost")
    wrapper = HTTPAuthSessionWrapper(portal, [credentialFactory])

    ## start listening on secure port
    reactor.listenSSL(securewebport, Site(wrapper), DefaultOpenSSLContextFactory(
                                      'keys/server.key', 'keys/server.crt'))

    ## start Twisted reactor ..
    reactor.run()
