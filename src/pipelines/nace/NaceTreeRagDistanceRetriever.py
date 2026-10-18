

from typing import Any

import json
import bisect
from time import sleep
from Job_Utils import loadJsonFor

from nace.NACE_Tree import  NaceTree,NaceRagVo

from llm_clients.LlmClientFactory import LlmClientFactory
from llm_clients.LlmClientOpenAiGpt import LlmClientOpenAiGpt

from RetrievalArgumentedGenerator import RetrievalArgumentedGenerator, VectorDbQueryResultVo


class NaceTreeRagDistanceRetriever:

    def __init__(self, rag:RetrievalArgumentedGenerator):
        #!! make configurable
        self.nace_tree_fn = "./docs/NACE_2_1.json"
        self.nace_embeddings_groups_fn = "./docs/NACE_2_1_groups_embeddings.json"
        self.nace_embeddings_classes_fn = "./docs/NACE_2_1_classes_embeddings.json"

        # Load the JSON data from a file
        with open(self.nace_tree_fn, "r") as f:
            json_obj = json.load(f)
        self.tree = NaceTree(json_obj)
        self.ncgroups_loaded = dict()
        
        #!! try/catch
        self.ncgroups_loaded = loadJsonFor(self.nace_embeddings_groups_fn)
        self.ncclasses_loaded = dict()
        #!! try/catch
        self.ncclasses_loaded = loadJsonFor(self.nace_embeddings_classes_fn)

        self.celex_nace_dist = dict()
        self.rag = rag


    def load_NACE_json(self)->NaceTree:
        # Load the JSON data from a file
        with open(self.nace_tree_fn, "r") as f:
            json_obj = json.load(f)
        tree = NaceTree(json_obj)
        return tree

    # Traverse and print the tree
    # for vo in tree.traverse():
    #    print(vo)


    #-----------------------------------------------------------------------------------------
    def determine_n_dump_nacetree_groups_embeddings(self,tree:NaceTree):
        # Load the JSON data from a file    
        ncgroups= tree.list_nacetree_groups()
        assert len(ncgroups) == 271
        
        llm = LlmClientOpenAiGpt(openai_organization=LlmClientFactory.get_openai_organization(),model='GPT-4.1', output_folder="./")

        ncdict = dict()
     
        
        for nc in ncgroups:
            print("nacetree group title:",nc.title)    
            sleep(1)
            nc.title_embeddings = llm.determine_embeddings4(nc.title)
            nc.childs = list() # avoid circular reference
            nc.parent = None            
          
            
            ncdict[nc.code] = nc
            

        with open(self.nace_embeddings_groups_fn, "w") as outfile:
            json.dump(ncdict, outfile, default=lambda o: o.__dict__, indent=4)

        return ncdict

    #-----------------------------------------------------------------------------------------
    def determine_n_dump_nacetree_classes_embeddings(self,tree:NaceTree):
        # Load the JSON data from a file    
        ncclasses= tree.list_nacetree_classes()
        assert len(ncclasses) == 613
        
        llm = LlmClientOpenAiGpt(openai_organization=LlmClientFactory.get_openai_organization(),model='GPT-4.1', output_folder="./")

        ncdict = dict()
     
        
        for nc in ncclasses:
            print("nacetree classes title: ",nc.title)    
            sleep(1)
            nc.title_embeddings = llm.determine_embeddings4(nc.title)
            nc.childs = list() # avoid circular reference
            nc.parent = None            
            
            nc.parents = None
            ncdict[nc.code] = nc
            

        with open(self.nace_embeddings_classes_fn, "w") as outfile:
            json.dump(ncdict, outfile, default=lambda o: o.__dict__, indent=4)

        return ncdict

    #-------------  
    def collect_embeddings4classes(self,ncClasses:list):
        rc = list()
        for k, v in self.ncclasses_loaded.items():
            for nc in ncClasses:
                if ( v['title'] == nc.title ):
                    print("title", v['title'])
                    rc.append(v)
        
        return rc

    #-----------------------------------------------------------------------------------------
    def iterate_all_nace_groups4(self,celex:str, ncgroups:dict)->dict:
        
        # could go to init if celex can be set later
        self.rag.set_celex(celex=celex)
        dist_min_sorted_list = list()
        dist_avg_sorted_list = list()
        celex_nace_dist = dict()
        for k, v in ncgroups.items():
            
            print("title", v['title'])
            
            resultVo: VectorDbQueryResultVo = self.rag.select_chunks_wth_embeddings(v['title_embeddings'], 100, 1.99)
            
            
            resultVo.rag_query = v['title']
            nc = self.tree.select_group_by_title(resultVo.rag_query )
            
            nc_dict = dict()
            nc_dict['group_code'] = nc.code     
            nc_dict['group_title'] = nc.title
            # "{\"group_code\": \""+  +\", \"group_title\": \"Specialised design activities\"}"
            nc_json =  json.dumps(nc_dict, default=lambda o: o.__dict__)
            nc_rag = NaceRagVo(resultVo, nc_json)
            

            # Insert using bisect.insort with key
            bisect.insort(dist_min_sorted_list,  nc_rag, key=lambda x: x.dist_min)
            bisect.insort(dist_avg_sorted_list,  nc_rag, key=lambda x: x.dist_avg)
            
        
        celex_nace_dist['celex'] = celex
        celex_nace_dist['nace_dist_min'] = [x.to_dict() for x in dist_min_sorted_list]
        celex_nace_dist['nace_dist_avg'] = [x.to_dict() for x in dist_avg_sorted_list]
        return celex_nace_dist
    
     #-----------------------------------------------------------------------------------------
    def iterate_all_nace_classes4(self,celex:str, ncclasses_embeddings:list)->dict:
        
        # could go to init if celex can be set later
        self.rag.set_celex(celex=celex)
        dist_min_sorted_list = list()
        dist_avg_sorted_list = list()
        celex_nace_dist = dict()
        for v in ncclasses_embeddings:
            
            print("title", v['title'])
            
            resultVo: VectorDbQueryResultVo = self.rag.select_chunks_wth_embeddings(v['title_embeddings'], 100, 1.99)
            
            
            resultVo.rag_query = v['title']
            nc = self.tree.select_class_by_title(resultVo.rag_query )
            
            nc_dict = dict()
            nc_dict['class_code'] = nc.code     
            nc_dict['class_title'] = nc.title
            nc_json =  json.dumps(nc_dict, default=lambda o: o.__dict__)
            nc_rag = NaceRagVo(resultVo, nc_json)
            

            # Insert using bisect.insort with key
            bisect.insort(dist_min_sorted_list,  nc_rag, key=lambda x: x.dist_min)
            bisect.insort(dist_avg_sorted_list,  nc_rag, key=lambda x: x.dist_avg)
            
        
        celex_nace_dist['celex'] = celex
        celex_nace_dist['nace_dist_min'] = [x.to_dict() for x in dist_min_sorted_list]
        celex_nace_dist['nace_dist_avg'] = [x.to_dict() for x in dist_avg_sorted_list]
        return celex_nace_dist
    



    def split_group_code(self, group_code:str)->str:
        # Split the group code by '.' and take the first part
        if '.' in group_code:
            return group_code.split('.')[0]
        else:
            return group_code

    def determine_position_of_human_idx_nace_group(self, celex_nace:list, human_idx:dict)->tuple:
        idx = 0    # -1 means top of the list is the human idx
        nace = dict()
        isfound = False
        for nace in celex_nace:
            if( not isfound):
                nc = json.loads(  nace.get('nace_json') )
                
                for hix in human_idx:
                    # normalize & consider group only
                    nc_group_code = self.split_group_code(group_code= nc.get('group_code') )
                    hix_group_code =self.split_group_code(group_code=hix.get('nace_group') )
                    
                    if nc_group_code == hix_group_code:
                        isfound = True
                        return (nace, idx, isfound)
                        
                    else:
                        idx += 1
            else:
                break
        return (nace, idx, isfound)


    def compare_wth_human_idx(self, celex_nace_dist:dict, human_idx:dict|Any )->tuple:
      
        nace_top_hit_dist_minf = float(celex_nace_dist['nace_dist_min'][0]['dist_min'] )
        NACE_rag_nace2idx_group_match_min_qa = False
        NACE_rag_nace2idx_group_match_avg_qa = False
        idx_min = -1
        idx_avg = -1
        nc2idx_dist_min = -1
        nc2idx_dist_avg = -1

        if( human_idx != 'NA'): 
            
            nace , idx_min, isfound = self.determine_position_of_human_idx_nace_group(celex_nace_dist['nace_dist_min'], human_idx)
            nc2idx_dist_min  = float(nace.get('dist_min',-1.0)) - nace_top_hit_dist_minf 
            NACE_rag_nace2idx_group_match_min_qa = isfound 
            

            #---
            
            nace, idx_avg, isfound  = self.determine_position_of_human_idx_nace_group(celex_nace_dist['nace_dist_avg'], human_idx)
            nc2idx_dist_avg  = float(nace.get('dist_avg',-1.0)) - nace_top_hit_dist_minf 
            NACE_rag_nace2idx_group_match_avg_qa = isfound  
            

        return ( str(idx_min),str(idx_avg), str(nc2idx_dist_min),str(nc2idx_dist_avg), str(NACE_rag_nace2idx_group_match_min_qa), str(NACE_rag_nace2idx_group_match_avg_qa) )
    

    # ------ MAIN ------------------------------       
if __name__ == '__main__':     
    
    retriever = NaceTreeRagDistanceRetriever(RetrievalArgumentedGenerator())
    # ncdict = retriever.determine_n_dump_nacetree_classes_embeddings(retriever.tree)

    retriever.determine_n_dump_nacetree_classes_embeddings(tree=retriever.tree)