# ./src/PropertyReader.py
'''
Read configurations from property file
'''
import configparser 
from sys import platform




# --------------------------------------------------------------------------
def determineOS()-> str:
    my_platform = "NA"
    if platform == "linux" or platform == "linux2":
        # linux
        my_platform = "linux"
    elif platform == "darwin":
        # OS X
        my_platform = "darwin"
    elif platform == "win32":
        # Windows...
        my_platform = "win"
    else: 
        my_platform = "other"
        my_platform = "other"
    return my_platform

# --------------------------------------------------------------------------
def loadConfigFrom(
    fn: str
) -> dict:
    config = configparser.ConfigParser()
    config.sections()
    config.read(fn)
    return config # type: ignore

# --------------------------------------------------------------------------
def loadDefaultConfig() -> dict:
    fn = determineOS() + "-main-pdf2img2txt.config"
    config = configparser.ConfigParser()
    config.sections()
    config.read(fn)
    return config # type: ignore

