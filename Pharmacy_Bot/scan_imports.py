import ast
from pathlib import Path
root = Path(__file__).resolve().parent
imports = {}
for path in root.rglob('*.py'):
    if path.name.startswith('.') or path.name == 'scan_imports.py':
        continue
    text = path.read_text(encoding='utf-8')
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        print('SKIP SYNTAX', path, e)
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                imports.setdefault(name.name.split('.')[0], set()).add(str(path.relative_to(root)))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.setdefault(node.module.split('.')[0], set()).add(str(path.relative_to(root)))
print('IMPORTS')
for name, files in sorted(imports.items()):
    print(name, '->', ','.join(sorted(files)))
