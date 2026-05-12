from collections import OrderedDict
from ctypes import *
import os
import queue
import threading
import time

import wx

import addonHandler
import buildVersion
import config
import extensionPoints
import languageHandler
import nvwave
import speech

from autoSettingsUtils.driverSetting import BooleanDriverSetting, DriverSetting
from autoSettingsUtils.utils import StringParameterInfo
from logHandler import log
from speech.commands import (
    BreakCommand,
    CharacterModeCommand,
    IndexCommand,
    LangChangeCommand,
    PitchCommand,
    RateCommand,
    SpeechCommand,
)
from synthDriverHandler import (
    LanguageInfo,
    SynthDriver,
    VoiceInfo,
    synthDoneSpeaking,
    synthIndexReached,
)

import globalPlugins.tiflotecniaVoices as pluginInstance

from globalPlugins.tiflotecniaVoices.utils import *
from . import lowLevel
from .lowLevel import languageDetection
from .lowLevel.structs import *

addonHandler.initTranslation()
synthInitialized = extensionPoints.Action()

LOCAL_DATA = os.path.join(os.path.dirname(__file__), "data")
GLOBAL_DATA = rf"{os.environ.get('PROGRAMDATA')}\{addonName}\Voices\Cerence"
SUPPORTED_SCRIPTS = {"cyrillic": languageDetection.CYRILLIC, "cjk": languageDetection.CJK, "latin": languageDetection.ALL_LATIN, "arabic": languageDetection.ARABIC, "hebrew": languageDetection.SINGLETONS["Hebrew"]}
PAUSES_MS = _("{0} MS")

QUALITY_MAP = {
    "embedded-compact": _("Lowest"),
                        "embedded-high": _("enhanced"),
                        "embedded-pro": _("Intermediate"),
    "embedded-premium": _("highest"),
    "premium-high": _("highest")
}

def getResourcePaths():
    resourcePaths = []
    resourcePaths.append(LOCAL_DATA)
    if os.path.exists(GLOBAL_DATA):
        resourcePaths.append(GLOBAL_DATA)
    return resourcePaths

def getAvailableResources():
    resources = OrderedDict()
    for l in lowLevel.getLanguageList():
        langCode = lowLevel.getLocaleNameFromTLW(l.szLanguageTLW.decode("utf-8"))
        if langCode is None:
            continue
        languageInfo = LanguageInfo(langCode)

        if not languageInfo.displayName:
            languageInfo.displayName = l.szLanguage
        resources[languageInfo] = []

        for v in lowLevel.getVoiceList(l.szLanguage):
            speechDBList = lowLevel.getSpeechDBList(l.szLanguage, v.szVoiceName)
            for entry in speechDBList:
                name = f'{v.szVoiceName.decode("utf-8")} ({QUALITY_MAP.get(entry, "")}) - {languageInfo.displayName}'
                voiceInfo = VoiceInfo(f'{v.szVoiceName.decode("utf-8")}_{entry}', name, languageInfo.id or None)
                resources[languageInfo].append(voiceInfo)

    return resources

def getAvailableLanguages():
    languages = set()
    for l in lowLevel.getNativeAndForeignLanguages():
        langCode = lowLevel.getLocaleNameFromTLW(l.decode("utf-8"))
        if langCode is None:
            continue
        languages.add(langCode)
    return languages

class BgThread(threading.Thread):

    def __init__(self, bgQueue):
        super().__init__()
        self._bgQueue = bgQueue
        self.setDaemon(True)
        self.start()

    def run(self):
        while True:
            task = self._bgQueue.get()
            if task is None:
                break
            try:
                task()
            except Exception:
                log.error("Error running task from queue", exc_info=True)
            self._bgQueue.task_done()

pcmBufLen = 8192
markBufSize = 100

class VECallback(object):

    def __init__(self, player, isSilence, onIndexReached):
        self._player = player
        self._isSilence = isSilence
        self._onIndexReached = onIndexReached
        self._pcmBuf = (c_byte * pcmBufLen)()
        self._markBuf = (VE_MARKINFO * markBufSize)()

    def __call__(self, instance, userData, message):
        try:
            outData = cast(message.contents.pParam, POINTER(VE_OUTDATA))
            messageType = message.contents.eMessage
            if self._isSilence.isSet() and messageType != VE_MSG_ENDPROCESS:
                return NUAN_E_TTS_USERSTOP
            elif messageType == VE_MSG_OUTBUFREQ:
                outData.contents.pOutPcmBuf = cast(self._pcmBuf, c_void_p)
                outData.contents.cntPcmBufLen = pcmBufLen
                outData.contents.pMrkList = cast(self._markBuf, POINTER(VE_MARKINFO))
                outData.contents.cntMrkListLen = markBufSize * sizeof(VE_MARKINFO)
            elif messageType == VE_MSG_OUTBUFDONE:
                if outData.contents.cntPcmBufLen > 0:
                    data = string_at(outData.contents.pOutPcmBuf, outData.contents.cntPcmBufLen)
                    self._player.feed(data)
                if self._isSilence.isSet():
                    return NUAN_E_TTS_USERSTOP
                for i in range(int(outData.contents.cntMrkListLen)):
                    if outData.contents.pMrkList[i].eMrkType == VE_MRK_BOOKMARK:
                        self._onIndexReached(int(outData.contents.pMrkList[i].szValue))
        except:
            log.error("callback", exc_info=True)
        return NUAN_OK

class ProcessText2Speech(object):

    def __init__(self, instance, text):
        self._instance = instance
        self._text = text

    def __call__(self):
        lowLevel.processText2Speech(self._instance, self._text)

class TtsSetParamList(object):

    def __init__(self, instance, *idAndValues):
        self._instance = instance
        self._idAndValues = idAndValues

    @property
    def instance(self):
        return self._instance

    @property
    def idAndValues(self):
        return self._idAndValues

    def __call__(self):
        lowLevel.setParamList(self._instance, *self._idAndValues)

class DoneSpeaking(object):

    def __init__(self, player, onIndexReached):
        self._player = player
        self._onIndexReached = onIndexReached

    def __call__(self):
        self._player.idle()
        self._onIndexReached(None)


class SynthDriver(SynthDriver):
    name = addonName
    description = addonSummary

    supportedSettings = [
        SynthDriver.VoiceSetting(),
        SynthDriver.RateSetting(),
        SynthDriver.PitchSetting(),
        SynthDriver.VolumeSetting(),
        BooleanDriverSetting("enableUnicodeLanguageSwitching", _("Automatically switch language based on unicode characters"), availableInSettingsRing=True),
        DriverSetting("waitfactor", _("&Pauses between sentences"), availableInSettingsRing=True),
    ]
    supportedCommands = {
        BreakCommand,
        CharacterModeCommand,
        IndexCommand,
        LangChangeCommand,
        PitchCommand,
        RateCommand,
    }
    supportedNotifications = {synthIndexReached, synthDoneSpeaking}

    @classmethod
    def check(cls):
        synthInitialized.notify()
        lowLevel.preinitialize()
        res = lowLevel.startAuthorization()
        if not res: return False
        res = lowLevel.setLicenseKey("")
        if not res: return False
        res = lowLevel.queryLicense()
        if not res: return False
        resources = getResourcePaths()
        if not resources:
            return False
        with lowLevel.preOpen(resources) as success:
            if not success:
                log.debugWarning("Error loading data", exc_info=True)
            return success

    def __init__(self):
        self.initialize()
        self._instanceCache = {}
        self._bgQueue = queue.Queue()
        self._bgThread = BgThread(self._bgQueue)

        self.outputDeviceSection = config.conf["speech"] if buildVersion.version_year < 2025 else config.conf["audio"]
        self._player = nvwave.WavePlayer(
            channels=1,
            samplesPerSec=22050,
            bitsPerSample=16,
            outputDevice=self.outputDeviceSection["outputDevice"],
            **({"buffered": True} if buildVersion.version_year < 2025 else {})
        )
        self._isSilence = threading.Event()
        self._veCallback = VE_CBOUTNOTIFY(VECallback(self._player, self._isSilence, self._onIndexReached))

        self._resources = getAvailableResources()
        self.availableLanguages = getAvailableLanguages()
        self._languageDetector = languageDetection.LanguageDetector([l.id for l in self._resources])
        self._enableUnicodeLanguageSwitching = False
        self._waitfactor = 1
        self._voice = self.getVoiceNameForLanguage(languageHandler.getLanguage())
        if self._voice is None:
            self._voice = list(self.availableVoices.keys())[0]

    def initialize(self):
        resources = getResourcePaths()
        if not resources:
            raise RuntimeError("no resources available")
        lowLevel.initialize(resources)

    def getVoiceInstance(self, voiceName):
        try:
            return self._instanceCache[voiceName]
        except KeyError:
            pass
        instance, name = lowLevel.open(voiceName, self._veCallback)
        log.debug(f"Created synth instance for voice {name}")
        self._instanceCache[name] = instance
        return instance

    def getVoiceNameForLanguage(self, language):
        if pluginInstance.instance is not None and pluginInstance.instance.specInit:
            synthConf = config.conf.get(addonName, None)
            if synthConf is not None:
                autoLangs = synthConf.get("langSwitching", None)
                if autoLangs is not None:
                    for configKey, script in SUPPORTED_SCRIPTS.items():
                        if not language.split("_")[0] in script: continue
                        scriptVoice = autoLangs.get(f"{configKey}Voice", None)
                        if scriptVoice is None: break
                        voice = scriptVoice.split("||")[0]
                        if voice == "undefined":
                            language = scriptVoice.split("||")[1]
                            break

                        if voice in self.availableVoices:
                            return voice

        for l, voices in self._resources.items():
            if l.id.startswith(language):
                return voices[0].id

    def terminate(self):
        self.cancel()
        try:
            for voiceName, instance in self._instanceCache.items():
                lowLevel.close(instance)
            self._instanceCache.clear()
            lowLevel.terminate()
        except RuntimeError:
            log.error("terminate", exc_info=True)
        self._bgQueue.put(None)
        self._bgThread.join()
        self._player.close()
        self._veCallback = None

    def speak(self, speechSequence):
        currentInstance = defaultInstance = self.voiceInstance
        currentLanguage = defaultLanguage = self.language
        defaultTlw = lowLevel.getTLWFromLocaleName(defaultLanguage)
        chunks = []
        hasText = False
        charMode = False
        if self._enableUnicodeLanguageSwitching:
            speechSequence = self._languageDetector.add_detected_language_commands(speechSequence, defaultLanguage)
        for command in speechSequence:
            if isinstance(command, str):
                command = command.strip()
                if not command:
                    continue
                if charMode or len(command) == 1:
                    command = command.lower()
                chunks.append(command.replace("\x1b", ""))
                hasText = True
            elif isinstance(command, IndexCommand):
                chunks.append(f"\x1b\\mrk={command.index}\\")
            elif isinstance(command, CharacterModeCommand):
                charMode = command.state
                s = "\x1b\\tn=spell\\" if command.state else "\x1b\\tn=normal\\"
                chunks.append(s)
            elif isinstance(command, LangChangeCommand):
                if command.lang == currentLanguage:
                    continue
                if command.lang is None:
                    currentInstance = defaultInstance
                    currentLanguage = defaultLanguage
                    chunks.append(f"\x1b\\lang={defaultTlw}\\")
                    continue
                currentLanguage = command.lang
                newVoiceName = self.getVoiceNameForLanguage(currentLanguage)
                if newVoiceName is None:
                    newInstance = defaultInstance
                else:
                    newInstance = self.getVoiceInstance(newVoiceName)
                    self._bgQueue.put(TtsSetParamList(newInstance, (Param.WAITFACTOR, self.getParameter(self.voiceInstance, Param.WAITFACTOR))))
                    self._bgQueue.put(TtsSetParamList(newInstance, (Param.SPEECHRATE, self.getParameter(self.voiceInstance, Param.SPEECHRATE))))
                    self._bgQueue.put(TtsSetParamList(newInstance, (Param.PITCH, self.getParameter(self.voiceInstance, Param.PITCH))))
                    self._bgQueue.put(TtsSetParamList(newInstance, (Param.VOLUME, self.getParameter(self.voiceInstance, Param.VOLUME))))
                if newInstance == currentInstance:
                    tlw = lowLevel.getTLWFromLocaleName(currentLanguage)
                    if tlw is not None:
                        chunks.append(f"\x1b\\lang={tlw}\\")
                    continue
                if hasText:
                    self._speak(currentInstance, chunks)
                    chunks = []
                    hasText = False
                currentInstance = newInstance
            elif isinstance(command, PitchCommand):
                pitch = self.getParameter(currentInstance, Param.PITCH)
                pitchOffset = self._percentToParam(command.offset, PITCH_MIN, PITCH_MAX) - PITCH_MIN
                chunks.append(f"\x1b\\pitch={pitch+pitchOffset}\\")
            elif isinstance(command, RateCommand):
                rate = self.getParameter(currentInstance, Param.SPEECHRATE)
                rateOffset = self._percentToParam(command.offset, RATE_MIN, RATE_MAX) - RATE_MIN
                chunks.append(f"\x1b\\rate={rate + rateOffset}\\")
            elif isinstance(command, BreakCommand):
                breakTime = max(1, min(command.time, 65535))
                chunks.append(f"\x1b\\pause={breakTime}\\")
            elif isinstance(command, SpeechCommand):
                log.debugWarning(f"Unsupported speech command: {command}")
            else:
                log.error(f"Unknown speech: {command}")
        if chunks:
            if currentLanguage != defaultLanguage:
                # Return to the default language of the voice.
                # This ensures that the language is always reset for a new sequence.
                chunks.append(f"\x1b\\lang={defaultTlw}\\")
            self._speak(currentInstance, chunks)
        self._bgQueue.put(DoneSpeaking(self._player, self._onIndexReached))

    def _speak(self, voiceInstance, chunks):
        text = speech.CHUNK_SEPARATOR.join(chunks).replace("  \x1b", "\x1b")
        self._bgQueue.put(ProcessText2Speech(voiceInstance, text))

    def cancel(self):
        taskList = []
        try:
            while True:
                task = self._bgQueue.get_nowait()
                self._bgQueue.task_done()
                if isinstance(task, (TtsSetParamList, DoneSpeaking)):
                    taskList.append(task)
        except queue.Empty:
            pass
        for task in taskList:
            self._bgQueue.put(task)
        self._isSilence.set()
        self._bgQueue.put(self._isSilence.clear)
        self._player.stop()
        self._bgQueue.join()

    def pause(self, switch):
        self._player.pause(switch)

    def _onIndexReached(self, index):
        if index is not None:
            synthIndexReached.notify(synth=self, index=index)
        else:
            synthDoneSpeaking.notify(synth=self)

    def getParameter(self, instance, paramId, type_=int):
        return self.getParameters(instance, (paramId, type_))[0]

    def getParameters(self, instance, *idAndTypes):
        taskList = []
        values = [None] * len(idAndTypes)

        try:
            while True:
                task = self._bgQueue.get_nowait()
                self._bgQueue.task_done()
                taskList.append(task)
        except queue.Empty:
            pass

        for task in filter(lambda t: isinstance(t, TtsSetParamList) and t.instance == instance, taskList):
            for id, value in task.idAndValues:
                for i, p in enumerate(idAndTypes):
                    if id == p[0]:
                        values[i] = value

        for task in taskList:
            self._bgQueue.put(task)
        if None not in values:
            return values
        return lowLevel.getParamList(instance, *idAndTypes)

    def _get_voiceInstance(self):
        return self.getVoiceInstance(self.voice)

    def _get_volume(self):
        return self.getParameter(self.voiceInstance, Param.VOLUME)

    def _set_volume(self, value):
        self._bgQueue.put(TtsSetParamList(self.voiceInstance, (Param.VOLUME, int(value))))

    def _get_rate(self):
        rate = self.getParameter(self.voiceInstance, Param.SPEECHRATE)
        return self._paramToPercent(rate, RATE_MIN, RATE_MAX)

    def _set_rate(self, value):
        rate = self._percentToParam(value, RATE_MIN, RATE_MAX)
        self._bgQueue.put(TtsSetParamList(self.voiceInstance, (Param.SPEECHRATE, rate)))

    def _get_pitch(self):
        pitch = self.getParameter(self.voiceInstance, Param.PITCH)
        return self._paramToPercent(pitch, PITCH_MIN, PITCH_MAX)

    def _set_pitch(self, value):
        pitch = self._percentToParam(value, PITCH_MIN, PITCH_MAX)
        self._bgQueue.put(TtsSetParamList(self.voiceInstance, (Param.PITCH, pitch)))

    def _getAvailableVoices(self):
        voices = []
        for items in self._resources.values():
            voices.extend(items)
        return OrderedDict([(v.id, v) for v in voices])

    def _get_voice(self):
        return self._voice

    def _set_voice(self, voiceName):
        if voiceName == self.voice: return
        if voiceName not in self.availableVoices:
            raise RuntimeError()
        self.cancel()
        self._voice = voiceName

    def _get_waitfactor(self):
        return str(self.getParameter(self.voiceInstance, Param.WAITFACTOR))

    def _set_waitfactor(self, value):
        self._bgQueue.put(TtsSetParamList(self.voiceInstance, (Param.WAITFACTOR, int(value))))

    def _get_availableWaitfactors(self):
        return {str(x): StringParameterInfo(str(x), PAUSES_MS.format(x*200)) for x in range(WAITFACTOR_MAX + 1)}

    def _get_language(self):
        return self.availableVoices[self.voice].language

    def _set_enableUnicodeLanguageSwitching(self, value):
        self._enableUnicodeLanguageSwitching = value

    def _get_enableUnicodeLanguageSwitching(self):
        return self._enableUnicodeLanguageSwitching
