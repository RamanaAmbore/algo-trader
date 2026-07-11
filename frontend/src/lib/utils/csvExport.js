/**
 * Export an array of objects to a browser-downloaded CSV file.
 * @param {Array<Record<string, any>>} rows
 * @param {Array<{header: string, key: string, format?: (v: any) => string}>} columns
 * @param {string} filename
 */
export function exportRowsToCsv(rows, columns, filename) {
  const escape = (v) => {
    const s = v == null ? '' : String(v);
    return s.includes(',') || s.includes('"') || s.includes('\n')
      ? `"${s.replace(/"/g, '""')}"`
      : s;
  };
  const header = columns.map(c => escape(c.header)).join(',');
  const body = rows.map(row =>
    columns.map(c => escape(c.format ? c.format(row[c.key]) : row[c.key])).join(',')
  ).join('\n');
  const blob = new Blob([header + '\n' + body], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
