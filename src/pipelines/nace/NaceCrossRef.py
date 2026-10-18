
import numpy as np
import csv
from nace.NaceTreeRagDistanceRetriever import NaceTreeRagDistanceRetriever
from RetrievalArgumentedGenerator import RetrievalArgumentedGenerator
# load Json




def compute_semantic_dist_4(nc1, nc2):
    # Convert lists to numpy arrays
    embedding_1 = np.array(nc1['title_embeddings'])
    embedding_2 = np.array(nc2['title_embeddings'])

    # Compute cosine similarity
    cos_sim = np.dot(embedding_1, embedding_2) / (
        np.linalg.norm(embedding_1) * np.linalg.norm(embedding_2)
    )

    # Compute semantic distance
    semantic_distance = 1 - cos_sim

    print("Cosine similarity:", cos_sim)
    print("Semantic distance:", semantic_distance)
    return semantic_distance

def list_ncgroup_code_title(ncgroups_loaded:dict)->list[str]:
    lst = []
    for k, nc in ncgroups_loaded.items():
        rc = nc['code'] +" "+ nc['title']
        lst.append(rc)
    return lst

retriever = NaceTreeRagDistanceRetriever(RetrievalArgumentedGenerator())
retriever.load_NACE_json()


# NACE_2_1_groups_embeddings.json
ncgroups_loaded = retriever.ncgroups_loaded
ncgroup_code_title_lst = list_ncgroup_code_title(ncgroups_loaded)
table = [['' for _ in range(len(ncgroup_code_title_lst) + 1)] for _ in range(len(ncgroup_code_title_lst) + 1)]

# Add headers
table[0][0] = 'NACE_ADJM' # Top-left corner empty
for idx, title in enumerate(ncgroup_code_title_lst, start=1):
    table[0][idx] = title  # Column headers
    table[idx][0] = title  # Row labels

i = 1
for k, nc1 in  ncgroups_loaded.items():
    j=1
    for k, nc2 in  ncgroups_loaded.items():
        print(nc1['title'])
        #if i<j:
        cs = compute_semantic_dist_4(nc1,nc2)
        table[i][j] = str(cs) if cs is not None else ''
            
        j += 1
    i += 1

# dump table to csv
with open('./docs/NACE_groups_semantic_distance.csv', mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerows(table)