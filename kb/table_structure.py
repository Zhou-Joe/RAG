"""Parse HTML table cells without assumptions about attribute order or quoting."""
from html.parser import HTMLParser


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
            if self.depth > 1:
                raise ValueError('嵌套表格需人工检查，不能静默丢弃内容')
        elif tag == 'tr':
            if self.row is not None:
                raise ValueError('表格行未闭合')
            self.row = []
        elif tag in ('td', 'th'):
            if self.row is None or self.cell is not None:
                raise ValueError('表格单元格结构异常')
            attrs = dict(attrs)
            spans = []
            for name in ('rowspan', 'colspan'):
                try:
                    value = int(attrs.get(name, '1'))
                except (ValueError, TypeError) as exc:
                    raise ValueError('表格跨度不是整数') from exc
                if not 1 <= value <= 1000:
                    raise ValueError('表格跨度超出支持范围')
                spans.append(value)
            self.cell = {'parts': [], 'rowspan': spans[0], 'colspan': spans[1], 'header': tag == 'th'}
        elif tag in ('br', 'p', 'div') and self.cell is not None:
            self.cell['parts'].append(' ')

    def handle_data(self, text):
        if self.cell is not None:
            self.cell['parts'].append(text)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            cell = self.cell
            cell['text'] = ' '.join(''.join(cell.pop('parts')).split())
            self.row.append(cell)
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.cell is not None:
                raise ValueError('表格单元格未闭合')
            self.rows.append(self.row)
            self.row = None
        elif tag == 'table':
            self.depth -= 1


def parse_table(source):
    parser = TableParser()
    parser.feed(source)
    parser.close()
    if parser.cell is not None or parser.row is not None or parser.depth:
        raise ValueError('表格 HTML 不完整')
    return parser.rows


def table_text(source):
    rows = parse_table(source)
    grid = {}
    width = 0
    for ri, cells in enumerate(rows):
        column = 0
        for cell in cells:
            rs, cs = cell['rowspan'], cell['colspan']
            while any((ri, column + offset) in grid for offset in range(cs)):
                column += 1
            if column + cs > 4096 or ri + rs > len(rows) or len(grid) + rs * cs > 200000:
                raise ValueError('表格跨度或尺寸异常，请检查原表')
            for row in range(ri, ri + rs):
                for col in range(column, column + cs):
                    if (row, col) in grid:
                        raise ValueError('表格单元格跨度冲突')
                    grid[row, col] = cell['text']
            column += cs
            width = max(width, column)
    rendered = []
    for ri, cells in enumerate(rows):
        if len(cells) == 1 and cells[0]['colspan'] == width and cells[0]['rowspan'] == 1:
            rendered.append(cells[0]['text'])
        else:
            rendered.append(' | '.join(grid.get((ri, col), '') for col in range(width)).rstrip(' |'))
    return '\n'.join(rendered)
