## Problem: 
my HeidelTime is working great but fail to identify text with missing blanks between the date components:
```
based on Regulation No.17 of
```  
**6February1962** 
```
(1), in particular on Articles6,7and8, 
```
This is a very common issue when dealing with text from OCR (Optical Character Recognition) or poorly parsed PDFs. Because HeidelTime is a rule-based system, it relies on specific, pre-defined regular expressions to identify temporal expressions. When the spaces are missing, the text no longer matches those default rules.You have two main ways to solve this: **Pre-processing the text** (the easiest and most common method) or **Adding custom rules to HeidelTime** (the advanced method).

Here is how to approach both.

### Method 1: Pre-process the text with Regex (Recommended)

The simplest and most robust way to handle this without digging into HeidelTime's source files is to clean your text before feeding it into the tagger. You can use a Regular Expression (Regex) to find concatenated dates and insert the missing spaces.

Here is an example using Python, assuming you are using a Python wrapper for HeidelTime:

Python

```
import re

text = """
based on Regulation No.17 of 6February1962 (1), in particular on Articles6,7and8,
with regard to the registration of an agreement made by the company Edmond Isbecque, Brussels, on 30January1963 pursuant to Article5 paragraph(1) of Regulation No.17(2), which it concluded on 28February1962 with the company Ludwig Hummel, Heitersheim/Baden (Germany), ‘and which was amended on 7April1965,
"""

# Regex pattern to find: 1 or 2 digits + letters (Month) + 4 digits
# (\d{1,2})   = Group 1: 1 or 2 digits (e.g., 6, 30)
# ([A-Za-z]+) = Group 2: One or more letters (e.g., February, April)
# (\d{4})     = Group 3: Exactly 4 digits (e.g., 1962, 1965)
pattern = re.compile(r'(\d{1,2})([A-Za-z]+)(\d{4})')

# Replace with the groups separated by spaces
cleaned_text = pattern.sub(r'\1 \2 \3', text)

print(cleaned_text)
```

**Output:**

> ... based on Regulation No.17 of **6 February 1962** (1) ... on **30 January 1963** ... on **28 February 1962** ... amended on **7 April 1965**...

Once you run this pre-processing step, you can pass `cleaned_text` directly into HeidelTime, and it will recognize the dates perfectly.

_Note: You can apply a similar regex pattern in Java, JavaScript, or any other language you are using to orchestrate your pipeline._

### Method 2: Modify HeidelTime's Rule Resources (Advanced)

If you require a strict pipeline where all logic must live inside HeidelTime itself, you can add a custom rule to its resource files.

HeidelTime reads its rules from `.txt` files located in its `resources` directory.

1. Navigate to the English rules directory: `heideltime-standalone/resources/english/rules/`
    
2. Open `rules_date.txt` (or create a custom rule file if your setup allows it).
    
3. You will see existing rules that look something like this: `RULENAME="Date_DayMonthYear" EXTRACTION="%reDay %reMonth %reYear" ...`
    
4. You would need to create a new rule that explicitly looks for the components without spaces. For example:
    
    Plaintext
    
    ```
    RULENAME="Date_DayMonthYear_NoSpaces"
    EXTRACTION="%reDay%reMonth%reYear"
    NORM_VALUE="%reYear-%normMonth-%normDay"
    ```
    

_(Note: The exact syntax depends on your specific version of HeidelTime, as the variable names like `%reMonth` refer to patterns defined in the `pattern` files within the same resources folder)._

**Why Method 1 is better:** Editing HeidelTime's internal rules requires recompiling the resource files or managing custom configurations, which can make updating HeidelTime to newer versions a headache later on. Pre-processing the text is much faster to implement and easier to maintain.


# Fix

~/a/celex-facts/src/pipelines/heideltime/qa/scan_dates.py 
 r"(\d{1,2})\s*(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{4})",
 but this matches for correct format with 2 blanks as well , so blanks = matching_date.count(" "); if blanks < 2:
 
celex_id: CELEX-31972D0397 -> 2 dates
celex_id: CELEX-31994D0210 -> 1 dates
celex_id: CELEX-31994D0599 -> 1 dates
celex_id: CELEX-32000D0400 -> 1 dates
celex_id: 33585_2_4 -> 1 dates
celex_id: 33708_6_7 -> 1 dates
celex_id: 38432_526_5 -> 1 dates
celex_id: 38628_498_2 -> 1 dates
celex_id: 38710_790_1 -> 1 dates
celex_id: 39165_1314_1 -> 1 dates
celex_id: 39168_757_6 -> 1 dates
celex_id: 39181_1908_8 -> 1 dates
celex_id: 39748_3911_5 -> 1 dates
celex_id: AT_40330_7947754_1178_9 -> 1 dates
celex_id: CELEX-31971D0268 -> 1 dates
celex_id: CELEX-31978D0163 -> 1 dates
celex_id: CELEX-31985D0206 -> 1 dates


4669 issues in 169 CELEX cases

 
fixed: malformed dates such as 23April1963 with .venv/bin/python src/pipelines/heideltime/qa/scan_dates.py