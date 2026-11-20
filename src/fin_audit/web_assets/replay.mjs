// Descriptions of recorded actions, never invented model thoughts.
const TABLES = {ledger:'Книга проводок', invoices:'Исходные счета', payments:'Банковские платежи', fx:'Курсы валют'};
export function replayFrames(reference) {
  if (!reference || !Array.isArray(reference.trajectory)) return [];
  return reference.trajectory.map(entry => {
    const action = entry.action;
    if (!action) return {step:entry.step, title:'Агент получил задачу', detail:'Документы, правила учёта и лимит действий. Эталон ему не передан.', table:null, terminal:false};
    if (action.action === 'list_records') {
      const records = entry.observation?.result?.records || [];
      return {step:entry.step, title:TABLES[action.args.table] || action.args.table, detail:`Получено записей: ${records.length}. Вызов list_records.`, table:action.args.table, terminal:false};
    }
    if (action.action === 'submit') return {step:entry.step, title:'Заключение отправлено оценщику', detail:`Заявлено ошибок: ${action.args.findings.findings.length}. Проверяются суммы и доказательства.`, table:null, terminal:true};
    return {step:entry.step, title:action.action, detail:'Действие записано в фактической траектории.', table:null, terminal:!!entry.terminated};
  });
}

export function recognitionTotal(book) {
  const revenue = book.policy.accounts.revenue;
  return book.ledger.filter(row => row.account === revenue).reduce((sum,row) => sum + row.credit_minor - row.debit_minor,0);
}
