"""Fix multi-line single-quoted strings in index.html."""
import re

with open('src/templates/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

def fix_multiline_strings(code):
    """Find single-quoted strings spanning multiple lines, collapse newlines to \\n."""
    result = []
    i = 0
    in_single_quote = False

    while i < len(code):
        ch = code[i]
        if not in_single_quote:
            if ch == "'":
                in_single_quote = True
            result.append(ch)
            i += 1
        else:
            if ch == "'":
                in_single_quote = False
                result.append(ch)
                i += 1
            elif ch in '\r\n':
                result.append('\\n')
                while i < len(code) and code[i] in '\r\n':
                    i += 1
            else:
                result.append(ch)
                i += 1
    return ''.join(result)

# Only fix the JS inside <script> tags
script_start = content.find('<script>')
script_end = content.find('</script>')
if script_start != -1 and script_end != -1:
    before = content[:script_start + 8]  # len('<script>') = 8
    script_body = content[script_start + 8:script_end]
    after = content[script_end:]

    fixed = fix_multiline_strings(script_body)
    content = before + fixed + after

    with open('src/templates/index.html', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Fixed all multi-line strings in script section')
else:
    print('ERROR: <script> tags not found')
