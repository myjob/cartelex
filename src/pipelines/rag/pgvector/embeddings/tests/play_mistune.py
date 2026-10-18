# https://mistune.lepture.com/en/latest/guide.html



import mistune

md_file="~/a/celex_docs/Russo_Schinkel_Guenster_Book/Russo_Schinkel_Günster_Carree_2010_Book-mineru.md"
# open and read the markdown file
with open(md_file, "r", encoding="utf-8") as f:
    markdown_text = f.read()

markdown = mistune.create_markdown(renderer='ast')
tokens = markdown(markdown_text)

stk = list(reversed(tokens))
after_heading = False
while stk:
    token = stk.pop()

    if token.get('type') == 'heading':
        after_heading = True
    elif after_heading and token.get('type') == 'text':
        print(token.get('raw', ''))
        after_heading = False

    if 'children' in token:
        for child in reversed(token['children']):
            stk.append(child)