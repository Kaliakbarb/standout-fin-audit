// Educational helpers consume public records only. No score or private truth.
export function localDate(timestamp, timezone) {
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(new Date(timestamp));
  const value = name => parts.find(part => part.type === name).value;
  return `${value('year')}-${value('month')}-${value('day')}`;
}

export function sourceOptions(book, type) {
  if (type === 'duplicate_invoice') return book.invoices.map(row => row.invoice_id);
  if (type === 'payment_allocation') return book.payments.map(row => row.payment_id);
  return book.ledger.filter(row => row.payment_id
    ? row.account === book.policy.accounts.cash
    : row.account === book.policy.accounts.receivable).map(row => row.row_id);
}

export function suggestEvidence(book, type, sourceId) {
  const proof = new Set(['policy']);
  let affected = [];
  if (type === 'duplicate_invoice') {
    const invoice = book.invoices.find(row => row.invoice_id === sourceId);
    if (!invoice) return {affected, evidence: [...proof]};
    proof.add(invoice.doc_id);
    affected = book.ledger.filter(row => row.invoice_id === sourceId && !row.payment_id && row.account === book.policy.accounts.receivable && row.debit_minor > 0).map(row => row.row_id);
    affected.forEach(id => proof.add(id));
  } else if (type === 'payment_allocation') {
    const payment = book.payments.find(row => row.payment_id === sourceId);
    if (!payment) return {affected, evidence: [...proof]};
    const invoice = payment.invoice_refs?.length === 1 ? book.invoices.find(row => row.invoice_id === payment.invoice_refs[0]) : null;
    affected = [payment.payment_id, ...(invoice ? [invoice.invoice_id] : [])];
    proof.add(payment.doc_id);
    if (invoice) proof.add(invoice.doc_id);
    book.ledger.filter(row => row.payment_id === sourceId).forEach(row => proof.add(row.row_id));
  } else {
    const row = book.ledger.find(row => row.row_id === sourceId);
    if (!row) return {affected, evidence: [...proof]};
    affected = [row.row_id];
    proof.add(row.row_id);
    const source = row.payment_id ? book.payments.find(doc => doc.payment_id === row.payment_id) : book.invoices.find(doc => doc.invoice_id === row.invoice_id);
    if (source) {
      proof.add(source.doc_id);
      if (type === 'fx_conversion' && source.currency !== book.manifest.base_currency) {
        const day = localDate(source.issued_at || source.timestamp, book.manifest.timezone);
        const fx = book.fx.find(rate => rate.date === day && rate.source_currency === source.currency && rate.target_currency === book.manifest.base_currency);
        if (fx) proof.add(fx.record_id);
      }
    }
  }
  return {affected, evidence: [...proof]};
}

export function buildLesson(book) {
  // This guided lesson is intentionally restricted to the open masking fixture.
  // It describes the records on screen; it never labels an unseen benchmark.
  const base = book.manifest.base_currency;
  const native = book.invoices.find(doc => doc.currency === base && doc.total_minor > 0);
  const foreign = book.invoices.find(doc => doc.currency !== base && doc.total_minor > 0);
  if (!native || !foreign) return null;
  const rows = book.ledger.filter(row => row.invoice_id === native.invoice_id && !row.payment_id && row.account === book.policy.accounts.receivable && row.debit_minor > 0);
  const foreignRow = book.ledger.find(row => row.invoice_id === foreign.invoice_id && !row.payment_id && row.account === book.policy.accounts.receivable);
  const day = localDate(foreign.issued_at, book.manifest.timezone);
  const rate = book.fx.find(row => row.date === day && row.source_currency === foreign.currency && row.target_currency === base);
  if (!foreignRow || !rate) return null;
  const recorded = rows.reduce((sum, row) => sum + row.debit_minor, 0);
  const expected = foreign.total_minor * Number(rate.rate) * 10 ** (book.manifest.currency_exponents[base] - book.manifest.currency_exponents[foreign.currency]);
  // The fixed lesson uses exact integer valuations. Other rounding belongs to
  // the production money module, never to this educational display helper.
  if (!Number.isSafeInteger(expected) || rows.length !== 2 || recorded <= native.total_minor) return null;
  const makeFinding = (type, source, value, explanation) => {
    const links = suggestEvidence(book, type, source);
    return {type, affected_ids: links.affected, claimed_amount_minor: value, currency: base, evidence_ids: links.evidence, explanation};
  };
  return {
    native, rows, recorded, duplicateAmount: recorded - native.total_minor,
    foreign, foreignRow, rate, expected,
    fxAmount: Math.abs(expected - foreignRow.debit_minor - foreignRow.credit_minor),
    duplicate: makeFinding('duplicate_invoice', native.invoice_id, recorded - native.total_minor, 'Один первичный счёт отражён в книге дважды. Лишняя сумма — превышение над счётом.'),
    fx: makeFinding('fx_conversion', foreignRow.row_id, Math.abs(expected - foreignRow.debit_minor - foreignRow.credit_minor), 'Сумма счёта пересчитана по курсу на дату документа и сопоставлена с основной записью в книге.'),
  };
}
