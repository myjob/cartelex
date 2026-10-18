"""
Some of the extraction tasks defined in fact_extractor.yaml have dependencies 
such as leniency_applicants require fact "company".  
I could run the batches x-times until the last conditions of the leaf in the dependency tree is satisfied. 
But I am looking for a smarter and simple way to model the dependency.
-->
This is a classic orchestration challenge. 
Since you're already using YAML to define your schema, you are essentially building a Directed Acyclic Graph (DAG).
Instead of guessing how many loops you need, use a Topological Sort. 
This algorithm takes a list of items with dependencies and returns a linear sequence 
where every dependency appears before the item that needs it.
"""

import yaml
from graphlib import TopologicalSorter

# Your YAML input
yaml_spec = """
- target_fact_name: leniency_case
  question: Did companies apply for leniency?
  pipeline: "hybrid_search"
  run_state_active: true
  predecessors: []
- target_fact_name: leniency_applicants
  question: Which companies applied for leniency?
  output_schema: "company"
  pipeline: "universal"
  predecessors: ["leniency_case", "company"]
  run_state_active: true

- target_fact_name: fines
  question: List all fines
  output_schema: "company, amount"
  pipeline: "universal"
  predecessors: ["company"]
  run_state_active: true

- target_fact_name: commissioner
  question: Who was the commissioner?
  pipeline: "universal"
  run_state_active: true
  predecessors: []

- target_fact_name: company
  question: List all company names mentioned.
  pipeline: "universal"
  run_state_active: true
  predecessors: []
"""

def get_execution_plan(spec_text):
    tasks = yaml.safe_load(spec_text)
    
    # 1. Map target_fact_name to its dependency list
    # Only include tasks where run_state_active is True
    graph = {
        task['target_fact_name']: set(task.get('predecessors', []))
        for task in tasks if task.get('run_state_active', False)
    }

    # 2. Initialize the Topological Sorter
    ts = TopologicalSorter(graph)
    
    try:
        # 3. Generate the linear execution order
        return list(ts.static_order())
    except Exception as e:
        return f"Circular Dependency Detected: {e}"

# Execute
plan = get_execution_plan(yaml_spec)
print(f"Optimized Batch Order: {plan}")