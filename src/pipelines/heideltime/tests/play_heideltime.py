# https://github.com/hmosousa/py_heideltime
from py_heideltime import heideltime

text = "Thurs August 31st - News today that they are beginning to evacuate the London children tomorrow. Percy is a billeting officer. I can't see that they will be much safer here."

# timexs = heideltime(text, language="English", document_type="news", dct="1939-08-31")
# print(timexs)

text = """
# Article 1  
The commitments set out in the Annex shall be binding on Football Association Premier League Limited.

# Article 2  
This decision shall apply from the date on which it is notified to Football Association Premier League Limited until 30 June 2013.

# Article 3  
The proceeding in this case are hereby brought to an end.

# Article 4  
This Decision is addressed to:  
Football Association Premier League Limited
30 Gloucester Place
London, W1U SPL
United Kingdom  
Brussels, 2 2 III 2006  
![](images/fcd0513e2f816049d99eea25d9546feefa73c63f15e15c8cac802c82f10ce8fd.jpg)  
For the Commission Neelie KROES Member of the Commission  
![](images/9af23377dd9885c26611ff09c45dc3142b6b6417ba3bf499e2056e4fc6887691.jpg)
"""

timexs = heideltime(text, language="English", document_type="news", dct="2006-03-22")

print(timexs)
