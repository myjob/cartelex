import os
from .Job_Utils import loadJsonFor, open_file_safely


def loadCelexFilterFrom(fileName: str) -> list[str]:
    ftxt = open_file_safely(fileName)
    return ftxt.split("\n")


def list_CELEX_keysIn(masterJsnOrg) -> list[str]:
    kys = list[str]()

    for k, v in list(masterJsnOrg["documents"].items()):
        kys.append(k)
    return kys


def iterate(masterJsnOrg):
    for k, v in list(masterJsnOrg["documents"].items()):
        print(k)
    return


def filterMasterJsonListFor(mstrJsnfName: str, fltrfName: str) -> list[dict]:

    lst = list()

    masterJsnOrg = loadJsonFor(mstrJsnfName)
    if not os.path.exists(fltrfName):
        print("Filter file does not exist -- see:" + fltrfName)
        raise FileNotFoundError("Filter file does not exist -- see:" + fltrfName)

    flrtCELEXList = loadCelexFilterFrom(fltrfName)

    if (
        flrtCELEXList is None
        or len(flrtCELEXList) == 0
        or (len(flrtCELEXList) == 1 and flrtCELEXList[0].strip() == "")
    ):
        # lst = list_CELEX_keysIn(masterJsnOrg)
        lst = list(masterJsnOrg["documents"].items())
    else:
        for fltr in flrtCELEXList:
            if fltr.strip() == "":
                print("Filter file contains empty lines -- see:" + fltrfName)
            else:
                mj = masterJsnOrg["documents"].get(fltr, "NA")

                if mj == "NA":
                    print(fltr, "Filter file contains keys with no hits for " + fltr)
                else:
                    lst.append((fltr, mj))
    return lst


def selectCelexKeysOutOffilterMasterJsonListFor(
    mstrJsnfName: str, fltrfName: str
) -> list[str]:
    rcList = list()
    celexDict = filterMasterJsonListFor(mstrJsnfName=mstrJsnfName, fltrfName=fltrfName)
    for k, v in celexDict:
        rcList.append(k)
    return rcList
