import hashlib
import csv

import json
from  langdetect import detect
from time import time
import shutil
import os
from os.path import exists
import re

# -------------------------------------------------------
def dumpDict2JsonFile(myDict, fnDictJson):
    with open(fnDictJson, "w",  newline='\r\n', encoding='utf8') as outfile:
        json.dump(myDict, outfile, indent=4, sort_keys=False, ensure_ascii=False)

# -------------------------------------------------------

def save2file(content:str, filepath:str):
    """ dump content to filepath """
    with open(filepath, 'w', encoding='utf-8') as outfile:
        outfile.write(content)
        
# -------------------------------------------------------
def loadJsonFor(
    fn: str
    ) -> dict:
    with open(fn, "r",   encoding='utf8') as infile:
        strngDict= json.load(infile)
        infile.close()
     # if Json is malformed, no dict but string; can also be a list of dict)    
    assert isinstance(strngDict, dict) or (isinstance(strngDict, list) and isinstance(strngDict[0], dict))
    return strngDict # type: ignore

# ----------------------------------------------------------------------

    
def read_csv_into_dict(file_path) -> list[dict]:
    """ Read a CSV file into a list of dictionaries. """
    data = []
    with open(file_path, 'r', newline='', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            data.append(row)
    return data


# ----------------------------------------------------------------------
    
def save_list_to_file(items: list, filepath: str, append: bool = False, encoding: str = 'utf-8') -> None:
    """Save a list to a text file, one item per line.

    Args:
        items: list of items (will be converted to str).
        filepath: destination file path.
        append: if True, append to existing file, otherwise overwrite.
        encoding: file encoding.
    """
    dirn = os.path.dirname(filepath)
    if dirn and (not exists(dirn)):
        os.makedirs(dirn, exist_ok=True)

    mode = 'a' if append else 'w'
    with open(filepath, mode, encoding=encoding, newline='') as f:
        for item in items:
            f.write(str(item).rstrip('\r\n') + '\n')


# ----------------------------------------------------------------------
    
def dump_dict_csv_to(file_path,listOfDict:list):
    with open(file_path, mode='w', newline='', encoding='utf-8') as csvfile:
    
        # If the dictionaries have the same keys, you can use the fieldnames from the first item
        fieldnames = listOfDict[0].keys() if listOfDict else []
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        # Write the header
        writer.writeheader()
        
        # Write the rows
        for row in listOfDict:
            writer.writerow(row)
    return


# -------------------------------------------------------
#!! backup shall be configurable
def backup_file(file_path:str,backup='./docs_archive/'):
    """ create a bakcup file with a time tag suffix to an optional directory"""
    # backup_file_path = file_path + '.' + str(int(time.time() * 1000))
    
    fnlst = file_path.split('/')
    if( len(fnlst)>2):
        fn = fnlst[len(fnlst)-1]
    else:
        fn = file_path
    backup_file_path =  backup + fn + ".%s" % time() 
    if(  exists(file_path)):
        shutil.copyfile(file_path, backup_file_path)           
           
#--------------------------------------------------------------------------

# !! required? 
def determineTxtLanguage(
    fn_path : str
) -> str:
    
    
    with open(fn_path, "r", encoding='utf8') as f:
        txt = f.read()
    
    if(len(txt)> 300):
        subtext = txt[int((len(txt)/2)):int(((len(txt)/2)+200))]
        txt = subtext
        
    lngAbbr = detect(txt)
    
    return lngAbbr
    

#--------------------------------------------------------------------------
#!! shall be named read rather than open            
def open_file(filepath):
    # with open(filepath, 'r', encoding='utf-8') as infile:
    with open(filepath, 'r', encoding='utf-8') as infile:
        return infile.read()

#--------------------------------------------------------------------------
            
def read_file(filepath):
    # with open(filepath, 'r', encoding='utf-8') as infile:
    with open(filepath, 'r', encoding='utf-8') as infile:
        return infile.read()



#--------------------------------------------------------------------------
            
def open_file_safely(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as infile:
            return infile.read()
    except FileNotFoundError:
        print('File does not exist')
    return ''    

#--------------------------------------------------------------------------
            
def singleColumnFileContent2List(filepath):
    txt =  open_file(filepath)
    lst = txt.split('\n')
    return lst
    

# ------------------------------------------------------------------     
# def findElementInTopicList(topicList:list, tpcName:str)->dict:

#!!  why is this not direct dic access 
def findElementInTopicList(topicList:dict, tpcName:str)->dict:
    """ raise Exception if no return value found """
    dct = dict()
    for e in list(topicList):
        if e["topic"] == tpcName:
            dct = e
            break
    if len(dct) == 0:
        raise ValueError( "No entry found in topic list for " +tpcName)
    
    return dct     
 

"""
   "president": {
            "sentences_extracted": [],
            "numof_sentences_extracted": 0,
            "gpt_numof_token": 0
        }
 """
def findElementInTopicListGraceFul(topicList:dict, tpcName:str)->dict:
    """ return empty dict if no return value found """
    dct = dict()
    for e in list(topicList):
        if e["topic"] == tpcName:
            dct = e
            break

    if len(dct) == 0:
        dct["topic"] = tpcName
        dct["sentences_extracted"] = []
        dct["numof_sentences_extracted"] = 0
        dct["gpt_numof_token"] = 0
        
    return dct     
 




# ------------------------------------------------------------------   
def extract_text(text, start_term, end_term):
    pattern = fr"{start_term}(.*?){end_term}"
    matches = re.findall(pattern, text, re.DOTALL)
    return matches
# print(extract_text(" For the Commission Mario Monti Member of the Commission","For the Commission",  "Member of the Commission"))

# print(extract_text(" For the Commission The president Walter HALLSTEIN","For the Commission",  "Member of the Commission"))



# ------------------------------------------------------------------   
def calculate_file_hash (fn:str):
    return hashlib.md5(open(fn,'rb').read()).hexdigest()

# ------------------------------------------------------------------   
def calculate_str_hash (strng:str):
    return hashlib.md5(strng.encode('utf-8')).hexdigest()

# ------------------------------------------------------------------   
def doesFileExistAndHasBytes(file_path:str)->bool:
    if(  exists(file_path)):
        if( os.path.getsize(file_path)>0):
           return True
    return False       

# ------------------------------------------------------------------   
def doesFileOrPathExist(file_path:str)->bool:
    return  exists(file_path)

# ------------------------------------------------------------------   
def _decode(s):
        try:
            return s.encode('utf-8').decode('unicode_escape')
        except Exception:
            return s

def _unicode_equal(str1, str2):
   
    return _decode(str1) == _decode(str2)


def map_corp2alias(data:list, partycorp:dict,mapping_attribute:str)->list:

    """
    Maps company names in the data to their corresponding alias_corp from the aliases dictionary.
    
    Args:
        data (list): List of dictionaries containing company information.
        aliases (dict): Dictionary containing company names and their aliases.
        
    Returns:
        list: Updated list with alias_corp added where applicable.
    """
    # Create a mapping from company name to alias_corp for quick lookup

    odd2partycorp_map = {item[ 'company' ]: item["alias_corp"] for item in partycorp["companies"]}
    partycorp2odd_map = {item["alias_corp"]: item[ 'company' ] for item in partycorp["companies"]}
    
    # Update data with alias_corp if applicable
    for entry in data:
        company = entry[mapping_attribute]
        
        if company in partycorp2odd_map or _decode(company) in partycorp2odd_map:
            entry["alias_corp"] = company
        
        elif company in odd2partycorp_map  or _decode(company) in odd2partycorp_map:
            entry["alias_corp"] = odd2partycorp_map[company]
        else:
            entry["alias_corp"] = 'NA'
    
    return data


