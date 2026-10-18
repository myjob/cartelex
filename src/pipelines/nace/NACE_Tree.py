from RetrievalArgumentedGenerator import  VectorDbQueryResultVo

""" ------------------
    load the NACE dictionary v2 from the docs folder and over acces to level 1 and 2 categoires
    the NACE dictionary is a json file with the following structure: list of dicts with "divisions" on level 1 and "groups" level 2 and classes on level 3 

        "code": "A",
        "title": "AGRICULTURE, FORESTRY AND FISHING",
        "divisions": [ 
            "code": "01",
            "title": "Crop and animal production, hunting and related service activities",
            "groups": [
                {
                    "code": "01.1",
                    "title": "Growing of non-perennial crops",
                    "classes": [
                        {
                            "code": "01.11",
                            "title": "Growing of cereals (except rice), leguminous crops and oil seeds",
                            "isic_code": "0111"
                        }, ..      
                },..
            ,..
            ]

------------------ """

class NaceVo(object):
    """ NaceVo is a value object for the NACE dictionary """

    def __init__(self, type:str, code:str, title:str,isic_code:str, childs:list, parent:object = None):
        self.type = type
        self.code = code
        self.title = title
        self.isic_code = isic_code
        self.childs = childs
        self.parent = parent
        self.title_embeddings = list[float]()
       
    def __repr__(self):
        return f"NaceVo(type={self.type}, code={self.code}, title={self.title})"        
    
    def __str__(self):
        return f"NaceVo(type={self.type},code={self.code}, title={self.title}, divisions_len={str(len(self.childs))})"
    
    # TypeError: Object of type NaceVo is not JSON serializable
    def to_dict(self):
        return {
            "type": self.type,
            "code": self.code,
            "title": self.title,
            "isic_code": self.isic_code,
            "childs": None,
            "parent": None
        }
    
class NaceRagVo:
    
    def __init__(self, rag_vo:VectorDbQueryResultVo, nace_json:str):

        # query spec
        self.rag_query=rag_vo.rag_query
        self.doc_type=rag_vo.doc_type
        self.max_hits=rag_vo.max_hits
        self.distance_crit=rag_vo.distance_crit

        # -- result
        self.tk = rag_vo.tk
        self.sentnumof=rag_vo.sentnumof
        
        self.dist_avg=rag_vo.dist_avg
        self.dist_min=rag_vo.dist_min
        self.dist_max=rag_vo.dist_max
        self.query_fit=rag_vo.query_fit    
        self.nace_json = nace_json

    def to_dict(self):
        return {
            'rag_query': self.rag_query,
            'doc_type': self.doc_type,
            'max_hits': self.max_hits,
            'distance_crit': self.distance_crit,
            'tk': self.tk,
            'sentnumof': self.sentnumof,
            'dist_avg': self.dist_avg,
            'dist_min': self.dist_min,
            'dist_max': self.dist_max,
            'query_fit': self.query_fit,
            'nace_json': self.nace_json,
        }
    def __repr__(self):
        return f"NaceRagVo(rag_query={self.rag_query},  max_hits={self.dist_min}, nace_json={self.nace_json})"


class NaceTree:
    def __init__(self, json_data):
        """Construct NaceTree from loaded JSON data (as Python objects)."""
        # We'll build a list of top-level NaceVo objects ("sections")
        self.root_sections = []
        for section in json_data:
            self.root_sections.append(self._build_vo(section, parent=None, node_type="section"))

    def _build_vo(self, node: dict, parent, node_type: str):
        """
        Recursively build a NaceVo from a node in the JSON dict.
        node_type is one of: 'section', 'division', 'group', 'class'
        """
        # Determine kids and next type
        key_map = {
            'section': ('divisions', 'division'),
            'division': ('groups', 'group'),
            'group': ('classes', 'class'),
            'class': (None, None),
        }
        
        kids_key, next_type = key_map[node_type]
        childs = []
        
        if kids_key and kids_key in node:
            for child in node[kids_key]:
                childs.append(self._build_vo(child, parent=None, node_type=next_type))
        
        # Set ISIC code only if available
        isic_code = node.get('isic_code', None)
        
        vo = NaceVo(
            type=node_type,
            code=node['code'],
            title=node['title'],
            isic_code=isic_code if isic_code is not None else "",
            childs=childs,
            parent=parent
        )
        # Set correct parent in childs
        for c in childs:
            c.parent = vo
            
        return vo

    def traverse(self):
        """Yield all nodes, recursively"""
        for s in self.root_sections:
            yield from self._traverse_node(s)
        
    def _traverse_node(self, vo):
        yield vo
        for c in vo.childs:
            yield from self._traverse_node(c)

    def list_nacetree_groups(self)->list[NaceVo]:
        
        group_nodes = list[NaceVo]()
        for vo in self.traverse():
            if vo.type == 'group':
                group_nodes.append(vo)
        return group_nodes
    

    def list_nacetree_groups_tuple_string(self)->str:
        group_nodes = list[str]()
        list_vo = self.list_nacetree_groups()
        for vo in list_vo:
            group_nodes.append( str(vo.code + ":" + vo.title) )
        return "\n".join(group_nodes)

    def list_nacetree_classes(self):
        
        class_nodes = []
        for vo in self.traverse():
            if vo.type == 'class':
                class_nodes.append(vo)
        return class_nodes
    

    def list_nacetree_classes_of_group(self, group_vo:NaceVo)->list:
        
        class_nodes = []
        for vo in group_vo.childs:
            if vo.type == 'class':
                class_nodes.append(vo)
        return class_nodes
    

    def list_nacetree_classes_of_group_by_title(self, title:str)->list:

        ncgroup : NaceVo = self.select_group_by_title(title)

        return ncgroup.childs
    


    def list_groups2classes_list_string(self, group_code:str)->str:
        """ list all classes in the NACE dictionary for a specific group code """
        class_nodes = []
        filtered_group = list(filter(lambda x: x.code == group_code, self.traverse()))
        for vo in filtered_group[0].childs:
            class_nodes.append( str(vo.code + ":" + vo.title) )
        
        return "\n".join(class_nodes)




    def select_group_by_title(self, title: str) -> NaceVo:
        
        list_vo = self.list_nacetree_groups()
        # lamda selecting list_vo by title
        group_nodes : list[NaceVo] = list(filter(lambda x: x.title == title, list_vo))
        
        return group_nodes[0] 
    

    def select_class_by_title(self, title: str) -> NaceVo:
        
        list_vo = self.list_nacetree_classes()
        # lamda selecting list_vo by title
        class_nodes : list[NaceVo] = list(filter(lambda x: x.title == title, list_vo))
        
        return class_nodes[0] 