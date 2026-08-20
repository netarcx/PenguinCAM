import sys

BS = chr(92)

def scan(path):
    src = open(path, encoding='utf-8').read()
    n = len(src)
    i = 0
    line = 1
    problems = []
    stack = []
    prev_sig = ''
    prev_word = ''
    while i < n:
        c = src[i]
        if c == '\n':
            line += 1; i += 1; continue
        if c in ' \t\r':
            i += 1; continue
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            j = src.find('\n', i)
            if j < 0:
                break
            i = j; continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            if j < 0:
                problems.append((line, 'unterminated block comment')); break
            line += src.count('\n', i, j)
            i = j + 2; continue
        if c in ('"', "'"):
            start = line; j = i + 1
            closed = False
            while j < n:
                d = src[j]
                if d == BS:
                    if j + 1 < n and src[j + 1] == '\n':
                        line += 1
                    j += 2; continue
                if d == '\n':
                    problems.append((start, 'RAW NEWLINE inside %s string: %r' % (c, src[i:j + 1][:100])))
                    line += 1
                    break
                if d == c:
                    closed = True; break
                j += 1
            if not closed and j >= n:
                problems.append((start, 'unterminated string'))
            i = j + 1
            prev_sig = 'x'
            continue
        if c == '`':
            start = line; j = i + 1
            closed = False
            while j < n:
                d = src[j]
                if d == BS:
                    j += 2; continue
                if d == '\n':
                    line += 1
                if d == '`':
                    closed = True; break
                j += 1
            if not closed:
                problems.append((start, 'unterminated template literal'))
            i = j + 1; prev_sig = 'x'; continue
        if c == '/':
            regex_ok = prev_sig in ('', '(', ',', '=', ':', '[', '!', '&', '|', '?', '{', '}',
                                    ';', '+', '-', '*', '~', '^', '%', '<', '>') \
                or prev_word in ('return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete',
                                 'void', 'case', 'do', 'else', 'yield', 'throw')
            if regex_ok:
                j = i + 1; incls = False; closed = False
                while j < n:
                    d = src[j]
                    if d == BS:
                        j += 2; continue
                    if d == '\n':
                        break
                    if d == '[':
                        incls = True
                    elif d == ']':
                        incls = False
                    elif d == '/' and not incls:
                        closed = True; break
                    j += 1
                if closed:
                    i = j + 1
                    while i < n and src[i].isalpha():
                        i += 1
                    prev_sig = 'x'; prev_word = ''
                    continue
            prev_sig = c; prev_word = ''; i += 1; continue
        if c in '([{':
            stack.append((c, line)); prev_sig = c; prev_word = ''; i += 1; continue
        if c in ')]}':
            pair = {')': '(', ']': '[', '}': '{'}[c]
            if not stack:
                problems.append((line, 'unmatched closing %s' % c))
            elif stack[-1][0] != pair:
                problems.append((line, 'mismatched %s; innermost open is %s from line %d'
                                 % (c, stack[-1][0], stack[-1][1])))
                stack.pop()
            else:
                stack.pop()
            prev_sig = c; prev_word = ''; i += 1; continue
        if c.isalnum() or c in '_$':
            j = i
            while j < n and (src[j].isalnum() or src[j] in '_$'):
                j += 1
            prev_word = src[i:j]; prev_sig = 'x'
            i = j; continue
        prev_sig = c; prev_word = ''
        i += 1
    for ch, ln in stack:
        problems.append((ln, 'UNCLOSED %s opened here' % ch))
    return problems


for p in sys.argv[1:]:
    probs = scan(p)
    print('==', p, '->', len(probs), 'problem(s)')
    for ln, m in probs:
        print('   line %d: %s' % (ln, m))
