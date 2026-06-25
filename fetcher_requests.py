<!DOCTYPE html>
<html lang="lv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BalticRadar</title>
<style>
  :root{--bg:#eef1f6;--card:#fff;--ink:#17243b;--mut:#6c7a90;--line:#e5e9f0;
    --accent:#1f6feb;--orange:#f26419;--up:#e5484d;--down:#1a9d54;
    --lv:#9d1d36;--lt:#f5b301;--ee:#0a7fd6;--shadow:0 6px 22px rgba(20,35,60,.08);}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 "Inter","Segoe UI",system-ui,Roboto,sans-serif}
  a{color:inherit}
  header{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid var(--line);
    box-shadow:0 1px 8px rgba(20,35,60,.04)}
  .bar{max-width:1240px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .logo{font-size:20px;font-weight:800;letter-spacing:-.3px;display:flex;align-items:center;gap:8px}
  .logo b{color:var(--accent)}
  .logo .ic{width:24px;height:24px}
  .status{font-size:13px;color:var(--mut);display:flex;align-items:center;gap:7px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--down);box-shadow:0 0 0 0 rgba(26,157,84,.6);animation:pulse 2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(26,157,84,.5)}70%{box-shadow:0 0 0 8px rgba(26,157,84,0)}100%{box-shadow:0 0 0 0 rgba(26,157,84,0)}}
  .pills{display:flex;flex-wrap:wrap;gap:6px}
  .pills button{background:#fff;border:1px solid var(--line);color:var(--ink);padding:7px 12px;
    border-radius:22px;cursor:pointer;font-size:13px;font-weight:500;transition:.15s;display:flex;align-items:center;gap:6px}
  .pills button:hover{border-color:#c5d0de}
  .pills button.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .langsel{background:#fff;border:1px solid var(--line);color:var(--ink);padding:7px 10px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;outline:none}
  .count{font-size:13px;color:var(--mut);font-weight:500}
  .notify{background:var(--orange);color:#fff;border:none;padding:8px 14px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer}
  .notify:hover{filter:brightness(.95)}
  #subform input,#subform select{background:#fff;border:1px solid var(--line);color:var(--ink);padding:10px 12px;border-radius:9px;font-size:14px;outline:none;width:100%}
  #subform input:focus,#subform select:focus{border-color:var(--accent)}
  .controls{max-width:1240px;margin:0 auto;padding:0 24px 14px;display:flex;gap:10px;flex-wrap:wrap}
  .controls input,.controls select{background:#fff;border:1px solid var(--line);color:var(--ink);
    padding:9px 12px;border-radius:10px;font-size:13.5px;outline:none;transition:.15s}
  .controls input:focus,.controls select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(31,111,235,.12)}
  .controls input[type=number]{width:100px}
  .controls .search{flex:1;min-width:180px}
  .topright{margin-left:auto;display:flex;gap:10px;align-items:center}
  .hero{background:linear-gradient(120deg,#13233f 0%,#1f4f8f 55%,#1f6feb 100%);color:#fff;text-align:center;padding:46px 24px 50px}
  .hero h1{margin:0 0 10px;font-size:34px;font-weight:800;letter-spacing:-.5px}
  .hero p{margin:0;font-size:16px;color:#cfe0f5;max-width:680px;margin-inline:auto}
  .layout{max-width:1240px;margin:0 auto;padding:22px 24px 80px;display:grid;grid-template-columns:255px 1fr;gap:24px;align-items:start}
  .sidebar{position:sticky;top:78px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:11px;box-shadow:var(--shadow)}
  .sidebar input,.sidebar select{background:#fff;border:1px solid var(--line);color:var(--ink);padding:9px 11px;border-radius:9px;font-size:13.5px;outline:none;width:100%}
  .sidebar input:focus,.sidebar select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(31,111,235,.12)}
  .row2{display:flex;gap:9px}.row2 input{flex:1;min-width:0}
  .mainbar{margin-bottom:14px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(225px,1fr));gap:16px}
  @media(max-width:760px){.layout{grid-template-columns:1fr}.sidebar{position:static}}
  .empty{color:var(--mut);text-align:center;padding:60px;grid-column:1/-1}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;
    cursor:pointer;display:flex;flex-direction:column;opacity:0;transform:translateY(14px);
    animation:rise .45s cubic-bezier(.2,.8,.2,1) forwards;transition:transform .18s,box-shadow .18s}
  @keyframes rise{to{opacity:1;transform:none}}
  .card:hover{transform:translateY(-4px);box-shadow:var(--shadow)}
  .ph{position:relative;aspect-ratio:16/10;background:#dfe5ee;overflow:hidden}
  .ph img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .5s,transform 6s ease-out}
  .ph img.show{opacity:1}
  .card:hover .ph img.show{transform:scale(1.07)}
  .ph .noimg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#9aa7b8;font-size:13px}
  .badge{position:absolute;top:11px;left:11px;background:var(--down);color:#fff;font-size:11px;fo