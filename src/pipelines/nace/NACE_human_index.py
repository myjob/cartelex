
import csv


def read_nace_human_index_csv_file(file_path)->dict:
    """
    Reads a CSV file and returns the contents as a list of dictionaries.
    Each row in the CSV file is represented as a dictionary with column headers as keys.
    """
    with open(file_path, mode='r', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        data = [row for row in reader]

    nace_human_index = dict()
    # csv.DictReader cuts header already

    line=1
    for row in data:

        if row['nace_code'] is None or row['nace_code'] == '':
            print(row['celex'],str(line),'ERROR: nace_code is empty')
            continue
        else:
            lst_nace_code = row['nace_code'].split(';') 
            lst_nace_desc = row['nace'].split(';') 
            if len(lst_nace_code) > 0 and  len(lst_nace_desc) > 0 and len(lst_nace_code) == len(lst_nace_desc):
                row_lst = list()
                for i in range(len(lst_nace_code)):
                    nace_code = lst_nace_code[i]
                    nace_EC = lst_nace_desc[i]
                    
                    row_lst.append( NaceHumanIndexVo(row['celex'], nace_EC, nace_EC, nace_code))
                    nace_human_index[row['celex']] = row_lst
            else:
                row_lst.append(  NaceHumanIndexVo(row['celex'], row['nace_EC'], row['nace'], row['nace_code']) )
                nace_human_index[row['celex']] = row_lst
        line += 1

    return nace_human_index


    return data

class NaceHumanIndexVo:
    """
    A class to represent the NACE Human Index.
    """

    def __init__(self, celex:str,nace_EC:str,nace_descr:str,nace_code:str ):
        """ celex,nace_EC,nace,nace_code """
        self.celex = celex
        self.nace_EC = nace_EC
        self.nace_descr = nace_descr
        self.nace_code = nace_code
        nace_onthology = nace_code.split('.')
        self.nace_division = nace_onthology[0]
        self.nace_group = nace_onthology[1] if len(nace_onthology) > 1 else None
        self.nace_class = nace_onthology[2] if len(nace_onthology) > 2 else None   


        return f"NaceHumanIndexVo(celex={self.celex}, nace_EC={self.nace_EC}, nace_descr={self.nace_descr}, nace_code={self.nace_code})"
    def __str__(self):
        return f"NaceHumanIndexVo(celex={self.celex}, nace_EC={self.nace_EC}, nace_descr={self.nace_descr}, nace_code={self.nace_code})"

#---------------------------------

def compare_human_index_list_with(
    nace_human_idx: list,
    nace_group_class: dict    
) -> tuple:
    """
    Compare the human index with the group and class data.
    """
    # Check if the human index matches the group and class data

    if  len(nace_human_idx)==1:
        return compare_human_index_record(nace_human_idx[0], nace_group_class)
    elif len(nace_human_idx) > 1:
        
        for idx in nace_human_idx:
            rc,desc,details = compare_human_index_record(idx, nace_group_class)
            if rc == 0 :
                s = "match of 1/" + str( len(nace_human_idx) )
                return (0, desc, s)
            elif rc == 5:    # 'warn: nace sub class not specified':
                return (0, desc, "match of 1/" + str( len(nace_human_idx) ) + " warn: nace sub class not specified" )
            elif rc == 4:    # 'warn: nace class not specified':
                return (0, desc, "match of 1/" + str( len(nace_human_idx) ) + " warn: nace class not specified")
            
    return (-1,"error: human index list is empty or not a list","")

def compare_human_index_record(
        idx: dict,
        nace_group_class: dict
)->tuple:        
    
    if idx['nace_group'] != nace_group_class['group_code'][:2]:
        s = idx['nace_group'] + " != " + nace_group_class['group_code'][:2]
        return (1,"misfit: idx group -- group_code error" , s)
    
    if idx['nace_class'] is None :
        # skip comparision on null
        return (0,"warn: nace class not specified","")
    
    elif nace_group_class['class_code'][3:] == '':
        return (0,"warn: nace sub class not specified" ,"")
    
    elif idx['nace_class'] != nace_group_class['class_code'][3:] and idx['nace_class'][:1] != nace_group_class['class_code'][3:]:
        s = idx['nace_class'] + " != " + nace_group_class['class_code'][3:]
        return (3, "misfit: sub class of  class_nace_group_classcode error" , s )
    
    #         13.10                  13.1                                 13.1
    if idx['nace_code'][2:] != nace_group_class['class_code'] and idx['nace_code'][2:6] != nace_group_class['class_code']:
        s = idx['nace_code'][2:] + " != " + nace_group_class['class_code'] 
        return (2,"misfit: idx code -- class_code error" , s )
    
    return (0, "match", "")


