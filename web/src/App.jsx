import React, { useEffect, useState } from "react";

const API = import.meta.env.BASE_URL.replace(/\/ui\/$/, "") + "/api";
const fmt = (n) => Number(n).toLocaleString("zh-CN", { minimumFractionDigits: 2 });
const Status = ({ s }) => <span className={"badge " + s}>{s}</span>;

/* ---------- Surface 1: 凭证 ---------- */
function VoucherSurface({ ledger }) {
  const [sel, setSel] = useState(null);
  const [detail, setDetail] = useState(null);
  return (
    <div className="split">
      <table>
        <thead><tr><th>凭证号</th><th>日期</th><th>状态</th><th>摘要</th></tr></thead>
        <tbody>
          {ledger.vouchers.map((v) => (
            <tr key={v.id} className={sel === v.id ? "sel" : ""}
                onClick={() => { setSel(v.id); fetch(`${API}/voucher/${v.id}`).then(r => r.json()).then(setDetail); }}>
              <td>{v.voucher_no}</td><td>{v.date}</td>
              <td><Status s={v.status} /></td><td>{v.summary}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {detail && (
        <div className="detail">
          <h3>{detail.voucher_no} · {detail.summary || "（无摘要）"}</h3>
          <table>
            <thead><tr><th>#</th><th>编码</th><th>科目</th><th>借方</th><th>贷方</th></tr></thead>
            <tbody>
              {detail.lines.map((l) => (
                <tr key={l.line_no}>
                  <td>{l.line_no}</td><td>{l.account_code}</td><td>{l.account_name}</td>
                  <td className="num">{fmt(l.debit)}</td><td className="num">{fmt(l.credit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ---------- Surface 2: 余额 ---------- */
function BalanceSurface({ ledger }) {
  return (
    <div>
      <h3>发生额投影（{ledger.current_period.year}-{String(ledger.current_period.month).padStart(2, "0")}）</h3>
      <table>
        <thead><tr><th>编码</th><th>科目</th><th>借方合计</th><th>贷方合计</th></tr></thead>
        <tbody>
          {ledger.balances.map((b) => (
            <tr key={b.code}>
              <td>{b.code}</td><td>{b.name}</td>
              <td className="num">{fmt(b.debit_total)}</td><td className="num">{fmt(b.credit_total)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- Surface 3: 报表 ---------- */
function ReportSurface({ lsId }) {
  const [r, setR] = useState(null);
  useEffect(() => { fetch(`${API}/ledger/${lsId}/reports`).then((x) => x.json()).then(setR); }, [lsId]);
  if (!r) return <p>加载中…</p>;
  const T = ({ title, rows, foot }) => (
    <div>
      <h3>{title}</h3>
      <table>
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}><td>{k}</td><td className="num">{fmt(v)}</td></tr>
          ))}
        </tbody>
      </table>
      {foot}
    </div>
  );
  return (
    <div className="reports">
      <T title="利润表" rows={[...r.income_statement.items.map((i) => [i.item, i.amount]),
        ["净利润", r.income_statement.net_profit]]} />
      <T title={`资产负债表 ${r.balance_sheet.balanced ? "✅ 平衡" : "❌ 不平衡"}`}
         rows={[
           ...r.balance_sheet.assets.items.map((i) => ["资产 · " + i.group, i.amount]),
           ["资产合计", r.balance_sheet.assets.total],
           ...r.balance_sheet.liabilities.items.map((i) => ["负债 · " + i.group, i.amount]),
           ["负债合计", r.balance_sheet.liabilities.total],
           ...r.balance_sheet.equity.items.map((i) => ["权益 · " + i.group, i.amount]),
           ["权益合计", r.balance_sheet.equity.total],
         ]} />
      <T title="现金流量表（直接法）"
         rows={[...r.cash_flow.items.map((i) => [i.item, i.amount]),
           ["经营活动净额", r.cash_flow.operating],
           ["投资活动净额", r.cash_flow.investing],
           ["筹资活动净额", r.cash_flow.financing],
           ["现金净增加额", r.cash_flow.net_increase]]} />
      <p className={r.reconcile.ok ? "ok" : "err"}>
        {r.reconcile.ok ? "✅ 账账核对一致" : "❌ 对账异常：" + r.reconcile.issues.map((i) => i.kind).join("、")}
      </p>
    </div>
  );
}

/* ---------- 主应用 ---------- */
function App() {
  const [ledgers, setLedgers] = useState(null);
  const [cur, setCur] = useState(null);
  const [tab, setTab] = useState("vouchers");
  useEffect(() => { fetch(`${API}/workspace`).then((r) => r.json()).then(setLedgers); }, []);
  useEffect(() => { if (cur) setTab("vouchers"); }, [cur]);
  if (!ledgers) return <p>加载中…</p>;
  return (
    <div>
      <h1>XErp <span className="badge v">v0.1</span></h1>
      <div className="picker">
        {ledgers.ledgers.map((l) => (
          <button key={l.id} className={cur && cur.id === l.id ? "on" : ""}
                  onClick={() => fetch(`${API}/ledger/${l.id}`).then((x) => x.json()).then(setCur)}>
            {l.name}
          </button>
        ))}
      </div>
      {cur && (
        <div>
          <div className="tabs">
            {["vouchers", "balances", "reports"].map((t) => (
              <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
                {{ vouchers: "凭证", balances: "余额", reports: "报表" }[t]}
              </button>
            ))}
          </div>
          {tab === "vouchers" && <VoucherSurface ledger={cur} />}
          {tab === "balances" && <BalanceSurface ledger={cur} />}
          {tab === "reports" && <ReportSurface lsId={cur.id} />}
        </div>
      )}
      {!cur && <p>← 选择一个账套开始</p>}
    </div>
  );
}

export default App;
