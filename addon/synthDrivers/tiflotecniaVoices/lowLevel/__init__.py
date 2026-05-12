import io
import os.path
import sys
import contextlib
from ctypes import *

from .structs import *
from .languages import getLocaleNameFromTLW, getTLWFromLocaleName

from globalPlugins.tiflotecniaVoices.utils import *

msvcrDll = None
ttsEngineLib = None
licenseLib = None
hSpeechClass = None
installResources = None


def veCheckForError(result, func, args):
    if result not in (NUAN_OK, NUAN_E_TTS_USERSTOP):
        raise VeError(result, f"{addonName} error: {func.__name__}: {result}")

def _newCopy(src):
    dst = type(src)()
    pointer(dst)[0] = src
    return dst

kernel32 = WinDLL("kernel32", use_last_error=True)
kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
kernel32.FreeLibrary.restype = wintypes.BOOL

def _freeLibrary(handle):
    if kernel32.FreeLibrary(handle) == 0:
        raise WindowsError()
    return True

def _initTtsEngineLib(path):
    ttsEngineLib = cdll.LoadLibrary(path)
    ttsEngineLib.initialize.errcheck = veCheckForError
    ttsEngineLib.initialize.restype = c_uint
    ttsEngineLib.getLastErrorMessage.restype = c_char_p
    ttsEngineLib.open.errcheck = veCheckForError
    ttsEngineLib.open.restype = c_uint
    ttsEngineLib.open.argtypes = (VE_HSAFE, c_void_p, c_void_p, POINTER(VE_HSAFE))
    ttsEngineLib.speak.errcheck = veCheckForError
    ttsEngineLib.speak.restype = c_uint
    ttsEngineLib.stop.errcheck = veCheckForError
    ttsEngineLib.stop.restype = c_uint
    ttsEngineLib.pause.errcheck = veCheckForError
    ttsEngineLib.pause.restype = c_uint
    ttsEngineLib.resume.errcheck = veCheckForError
    ttsEngineLib.resume.restype = c_uint
    ttsEngineLib.setParamList.errcheck = veCheckForError
    ttsEngineLib.setParamList.restype = c_uint
    ttsEngineLib.getParamList.errcheck = veCheckForError
    ttsEngineLib.getParamList.restype = c_uint
    ttsEngineLib.getLanguages.errcheck = veCheckForError
    ttsEngineLib.getLanguages.restype = c_uint
    ttsEngineLib.getVoices.restype = c_uint
    ttsEngineLib.getVoices.errcheck = veCheckForError
    ttsEngineLib.getSpeechDbs.restype = c_uint
    ttsEngineLib.getSpeechDbs.errcheck = veCheckForError
    ttsEngineLib.close.restype = c_uint
    ttsEngineLib.close.errcheck = veCheckForError
    ttsEngineLib.uninitialize.restype = c_uint
    ttsEngineLib.uninitialize.errcheck = veCheckForError
    ttsEngineLib.setCallback.errcheck = veCheckForError
    ttsEngineLib.setCallback.restype = c_uint
    ttsEngineLib.resourceLoad.errcheck = veCheckForError
    ttsEngineLib.getProductVersion.restype = c_uint
    ttsEngineLib.getProductVersion.errcheck = veCheckForError
    ttsEngineLib.getAdditionalProductInfo.restype = c_uint
    ttsEngineLib.getAdditionalProductInfo.errcheck = veCheckForError
    ttsEngineLib.resourceLoad.restype = c_uint
    ttsEngineLib.getInterfaces.errcheck = veCheckForError
    ttsEngineLib.getInterfaces.restype = c_uint
    ttsEngineLib.getInterfaces.argtypes = (POINTER(VE_INSTALL), POINTER(VPLATFORM_RESOURCES))
    ttsEngineLib.releaseInterfaces.errcheck = veCheckForError
    ttsEngineLib.releaseInterfaces.restype = c_uint
    return ttsEngineLib

_basePath = os.path.dirname(__file__)
is_x64 = sys.maxsize > 2**32
libPath = os.path.join(_basePath, "lib", "x64" if is_x64 else "x86")
def preinitialize():
    global msvcrDll, ttsEngineLib, licenseLib, hSpeechClass, installResources
    msvcrDll = cdll.LoadLibrary(os.path.join(libPath, "msvcp140.dll"))
    licenseLib = cdll.LoadLibrary(os.path.join(libPath, f"licenseManager_{addonName}.dll"))
    ttsEngineLib = _initTtsEngineLib(os.path.join(libPath, f"runatts_{addonName}.dll"))

def initialize(resourcePaths):
    global msvcrDll, ttsEngineLib, licenseLib, hSpeechClass, installResources
    if ttsEngineLib is None:
        preinitialize()
    res = startAuthorization()
    if not res:
        return False
    resourcePaths.insert(0, _basePath)
    installResources = VE_INSTALL()
    installResources.fmtVersion = VE_CURRENT_VERSION
    installResources.pBinBrokerInfo = None
    platformResources = VPLATFORM_RESOURCES()
    platformResources.fmtVersion = VPLATFORM_CURRENT_VERSION
    platformResources.u16NbrOfDataInstall = c_ushort(len(resourcePaths))
    platformResources.apDataInstall = (c_wchar_p * (len(resourcePaths)))()
    for i, path in enumerate(resourcePaths):
        platformResources.apDataInstall[i] = c_wchar_p(path)
    platformResources.pDatPtr_Table = None
    ttsEngineLib.getInterfaces(byref(installResources), byref(platformResources))

    hSpeechClass = VE_HSAFE()
    ttsEngineLib.initialize(byref(installResources), byref(hSpeechClass))

def open(voice, callback):
    global installResources
    instance = VE_HSAFE()
    ttsEngineLib.open(hSpeechClass, installResources.hHeap, installResources.hLog, byref(instance))

    if voice is None:
        language = getLanguageList()[0].szLanguage
        voice = getVoiceList(language)[0].szVoiceName
    else:
        voiceName, vop = voice.split("_")
    setParamList(instance,
        (Param.VOICE, voiceName),
        (Param.VOICE_OPERATING_POINT, vop),
        (Param.MARKER_MODE, VE_MRK_ON),
        (Param.INITMODE, VE_INITMODE_LOAD_ONCE_OPEN_ALL),
        (Param.TEXTMODE, VE_TEXTMODE_STANDARD),
        (Param.TYPE_OF_CHAR, VE_TYPE_OF_CHAR_UTF8),
        (Param.READMODE, ReadMode.SENT),
        (Param.FREQUENCY, 22),
        (Param.DISABLE_FINAL_SILENCE, 0),
    )

    outDevInfo = VE_OUTDEVINFO()
    outDevInfo.pfOutNotify  = callback
    ttsEngineLib.setCallback(instance, byref(outDevInfo))
    return (instance, voice)

def close(instance):
    ttsEngineLib.close(instance)

def startAuthorization():
    res = ttsEngineLib.startAuthorization()
    if res != 0:
        return False
    return True

def setLicenseKey(licenseKey):
    res = ttsEngineLib.setLicenseKey(licenseKey)
    if res != 0:
        return False
    return True

def generateAuthorizationData(path):
    res = ttsEngineLib.generateAuthorizationData(hSpeechClass, path)
    if res != 0:
        return False
    return True

def queryLicense():
    res = ttsEngineLib.queryLicense(hSpeechClass)
    if res != 0:
        return False
    return True

def registerLicense(path="", offline=False):
    res = ttsEngineLib.registerLicense(hSpeechClass, path, offline)
    if res != 0:
        return False
    return True

def unregisterLicense(path=""):
    res = ttsEngineLib.unregisterLicense(None, path)
    if res != 0:
        return False
    return True

def isActivationOffline():
    res = ttsEngineLib.isActivationOffline()
    if res == 0:
        return True
    return False

def getLastErrorMessage():
    try:
        return ttsEngineLib.getLastErrorMessage().decode()
    except UnicodeDecodeError:
        return "Unknown error"

def terminate():
    global msvcrDll, ttsEngineLib, licenseLib, hSpeechClass, installResources
    if hSpeechClass is not None:
        try:
            ttsEngineLib.uninitialize(hSpeechClass)
        except VeError:
            pass
    try:
        ttsEngineLib.releaseInterfaces(byref(installResources))
    except TypeError:
        pass
    hSpeechClass = None
    installResources = None
    try:
        _freeLibrary(ttsEngineLib._handle)
        _freeLibrary(licenseLib._handle)
        _freeLibrary(msvcrDll._handle)
    finally:
        ttsEngineLib = None
        licenseLib = None
        msvcrDll = None

def processText2Speech(instance, text):
    text = text.encode("utf-8", "replace")
    inText = VE_INTEXT()
    inText.eTextFormat = VE_NORM_TEXT # this is the only supported format...
    inText.cntTextLength = c_size_t(len(text))
    inText.szInText = cast(c_char_p(text), c_void_p)
    ttsEngineLib.speak(instance, byref(inText))

def setParamList(instance, *idAndValues):
    size = len(idAndValues)
    params = (VE_PARAM * size)()
    for i, pair in enumerate(idAndValues):
        params[i].eID = pair[0]
        if isinstance(pair[1], int):
            params[i].uValue.usValue = c_ushort(pair[1])
        else:
            params[i].uValue.szStringValue = pair[1].encode("utf-8")
    ttsEngineLib.setParamList(instance, params, c_ushort(size))

def getParamList(instance, *idAndTypes):
    size = len(idAndTypes)
    params = (VE_PARAM * size)()
    for i, pair in enumerate(idAndTypes):
        params[i].eID = pair[0]
    ttsEngineLib.getParamList(instance, params, c_ushort(size))
    values = []
    for i, pair in enumerate(idAndTypes):
        values.append(params[i].uValue.usValue if pair[1] is int else params[i].uValue.szStringValue.decode("utf-8"))
    return values

def getLanguageList():
    nItems = c_ushort()
    ttsEngineLib.getLanguages(hSpeechClass, None, byref(nItems))
    langs = (VE_LANGUAGE * nItems.value)()
    ttsEngineLib.getLanguages(hSpeechClass, langs, byref(nItems))
    languages = []
    for i in range(nItems.value):
        languages.append(_newCopy(langs[i]))
    return languages

def getVoiceList(languageName):
    nItems = c_ushort()
    ttsEngineLib.getVoices(hSpeechClass, c_char_p(languageName), None, byref(nItems))
    voiceInfos = (VE_VOICEINFO * nItems.value)()
    ttsEngineLib.getVoices(hSpeechClass, c_char_p(languageName), byref(voiceInfos), byref(nItems))
    l = []
    for i in range(nItems.value):
        l.append(_newCopy(voiceInfos[i]))
    return l

def getSpeechDBList(languageName, voiceName):
    nItems = c_ushort()
    ttsEngineLib.getSpeechDbs(hSpeechClass, c_char_p(languageName), c_char_p(voiceName), None, byref(nItems))
    speechDBInfos = (VE_SPEECHDBINFO * nItems.value)()
    ttsEngineLib.getSpeechDbs(hSpeechClass, c_char_p(languageName), c_char_p(voiceName), byref(speechDBInfos), byref(nItems))
    voiceModels = []
    for i in range(nItems.value):
        voiceModels.append(speechDBInfos[i].szVoiceOperatingPoint.decode("utf-8"))
    return voiceModels

def getNativeAndForeignLanguages():
    languages = set()
    for l in getLanguageList():
        languages.add(l.szLanguageTLW)
        for v in getVoiceList(l.szLanguage):
            if not v.szForeignLanguages:
                continue
            for fl in v.szForeignLanguages.split(b','):
                languages.add(fl.upper())
    return languages


def resourceLoad(contentType, content, instance):
    length = len(content)
    hout = VE_HSAFE()
    ttsEngineLib.resourceLoad(instance, contentType, length, content, byref(hout))
    return hout

def getAdditionalProductInfo():
    additionalProductInfo = VE_ADDITIONAL_PRODUCTINFO()
    ttsEngineLib.getAdditionalProductInfo(byref(additionalProductInfo))
    return additionalProductInfo

def getProductVersion():
    productVersion = VE_PRODUCT_VERSION()
    ttsEngineLib.getProductVersion(byref(productVersion))
    return productVersion

@contextlib.contextmanager
def preOpen(resourcePaths):
    wasInit = hSpeechClass is not None
    success = False
    try:
        if not wasInit:
            initialize(resourcePaths)
        success = True
    except VeError:
        pass
    try:
        yield success
    finally:
        if not wasInit:
            terminate()
