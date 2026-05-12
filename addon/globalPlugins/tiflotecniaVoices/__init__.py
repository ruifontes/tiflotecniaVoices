import ctypes
import wx
import addonHandler
import synthDriverHandler
import tones
import config
import core
import extensionPoints
import globalPluginHandler
import globalVars
import gui
from gui.settingsDialogs import SettingsPanel
import languageHandler
import os
from synthDriverHandler import getSynth
from synthDrivers.tiflotecniaVoices.lowLevel import languages
from synthDrivers.tiflotecniaVoices.lowLevel import languageDetection
import synthDrivers.tiflotecniaVoices.lowLevel as ttsEngine
import synthDrivers.tiflotecniaVoices as tts
from winAPI import messageWindow
from .interface import Interface
from .license_manager import LicenseManager
from .utils import *

addonHandler.initTranslation()

specInitialized = extensionPoints.Action()

confspec = {
'langSwitching': {
'latinVoice': 'string(default="undefined||en")',
'cyrillicVoice': 'string(default="undefined||ru")',
'cjkVoice': 'string(default="undefined||ja")',
'arabicVoice': 'string(default="undefined||ar")',
'hebrewVoice': 'string(default="undefined||he")'
}
}


class VoicesSettingsPanel(SettingsPanel):
    title = addonSummary
    SUPPORTED_SCRIPTS = [
        {"id": "latin", "name": _("Latin"), "script": languageDetection.ALL_LATIN},
        {"id": "cyrillic", "name": _("Cyrillic"), "script": languageDetection.CYRILLIC},
        {"id": "cjk", "name": _("CJK"), "script": languageDetection.CJK},
        {"id": "arabic", "name": _("Arabic"), "script": languageDetection.ARABIC},
        {"id": "hebrew", "name": _("Hebrew"), "script": languageDetection.SINGLETONS["Hebrew"]}
    ]

    def makeSettings(self, sizer):
        self.sHelper = gui.guiHelper.BoxSizerHelper(self, sizer=sizer)
        if not instance.licenseManager.checkLicenseValidity():
            return
        with instance.licenseManager._engineInit():
            if not getSynth().name == addonName:
                resources = tts.getResourcePaths()
                if not resources:
                    raise RuntimeError("no resources available")
                ttsEngine.initialize(resources)

            for script in self.SUPPORTED_SCRIPTS:
                _id = script["id"]
                name = script["name"]
                setattr(self, f"{_id}Choice", self.createVoiceChoice(name, script["script"], f"{_id}Voice"))

    def onSave(self):
        for script in self.SUPPORTED_SCRIPTS:
            _id = script["id"]
            self.saveVoiceToConf(getattr(self, f"{_id}Choice"), f"{_id}Voice")

    def createVoiceChoice(self, name, script, config_key):
        voices = self.getVoicesByScript(script)
        if voices:
            choice = self.sHelper.addLabeledControl(_("Voice for {name} characters:").format(name=name), wx.Choice, choices=[])
            [choice.Append(f"{x['voiceName']} ({tts.QUALITY_MAP.get(x['quality'], '')}) - {x['languageName']}", x) for x in voices]
            savedVoice = config.conf[addonName]["langSwitching"].get(config_key, None)
            try:
                savedVoice = savedVoice.split("||")
                vName, vop = savedVoice[0].split("_")
                choice.Select(choice.FindString(f"{vName} ({tts.QUALITY_MAP.get(vop, '')}) - {languageHandler.getLanguageDescription(savedVoice[1])}")) if not "undefined" in savedVoice else choice.Select(0)
            except (AttributeError, ValueError, KeyError) as e:
                choice.Select(0)
            return choice
        else:
            return None

    def saveVoiceToConf(self, choice, config_key):
        if choice is not None and choice.GetSelection() != wx.NOT_FOUND:
            clientData = choice.GetClientData(choice.GetSelection())
            config.conf[addonName]["langSwitching"][config_key] = f"{clientData['voiceName']}_{clientData['quality']}||{clientData['languageCode']}"

    def getVoicesByScript(self, script):
        langsList = []
        langs = ttsEngine.getLanguageList()
        for lang in langs:
            lngCode = languages._vautoTLWToLocaleNames[lang.szLanguageTLW.decode()]
            if  lngCode.split("_")[0] not in script:
                continue
            voices = ttsEngine.getVoiceList(lang.szLanguage)
            for voice in voices:
                speechDBList = ttsEngine.getSpeechDBList(lang.szLanguage, voice.szVoiceName)
                for entry in speechDBList:
                    langsList.append({"voiceName": voice.szVoiceName.decode(), "quality": entry, "languageCode": lngCode, "languageName": languageHandler.getLanguageDescription(lngCode)})
        return langsList


instance = None
class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    def __init__(self, *args, **kwargs):
        global instance
        super().__init__(*args, **kwargs)
        user32 = ctypes.windll.user32
        self.msgid = user32.RegisterWindowMessageW("TIFLOTECNIA_VOICES_UPDATED")
        messageWindow.pre_handleWindowMessage.register(self.notifier)
        self.licenseManager = LicenseManager()
        self.interface = Interface(self, self.licenseManager)
        self.specInit = False
        instance = self
        tts.synthInitialized.register(self.initSpec)
        self.initSpec()
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(VoicesSettingsPanel)
        self.interface.createMenu()
        self.checkLicense()

    def terminate(self):
        super().terminate()
        tts.synthInitialized.unregister(self.initSpec)
        self.interface.removeMenu()
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(VoicesSettingsPanel)

    def checkLicense(self):
            # Do not show activation dialog neither voice manager during install or silent install
        if globalVars.appArgs.install or globalVars.appArgs.installSilent:
                return
        if not self.licenseManager.checkLicenseValidity():
            self.interface.showActivationDialog()
        else:
            if not self.checkData():
                wx.CallLater(2000, self.interface.onManageVoices)

    def checkData(self):
        currentDir = os.path.dirname(os.path.abspath(__file__))
        localDataDir = os.path.join(currentDir, "..", "..", "synthDrivers", addonName, "data")
        globalDataDir = os.path.join(os.getenv("ALLUSERSPROFILE"), addonName, "Voices", "Cerence")
        localData = []
        globalData = []
        if os.path.exists(localDataDir):
            localData = [item for item in os.listdir(localDataDir) if item != "common"]
        if os.path.exists(globalDataDir):
            globalData = [item for item in os.listdir(globalDataDir) if item != "common"]
        data = localData + globalData
        return bool(data)

    def initSpec(self):
        if self.specInit: return
        config.conf.spec[addonName] = confspec
        self.specInit = True
        specInitialized.notify()

    def notifier(self, msg, wParam, lParam):
        if msg == self.msgid:
            synth = synthDriverHandler.getSynth()
            if synth.name == addonName:
                synthDriverHandler.setSynth(synth.name)
