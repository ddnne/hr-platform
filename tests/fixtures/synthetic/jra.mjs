export const race='jra:20000101:5:1',entrants=[1,2,10,11],frames={1:1,2:2,10:3,11:3};
export const roster={entrants,frames};
const classes={win_place:'tanpuku',frame_quinella:'waku',quinella:'umaren',wide:'wide',exacta:'umatan',trio:'fuku3',trifecta:'tan3'};
export function jraBody(page,{phase='最終オッズ',omit=null,value='12.3',course='芝・左',venue='東京',day='2000年1月1日',category='3歳以上',raceName='合成競走'}={}){
 let body=`<h1><img alt="合成ロゴ" /></h1><h1>合成オッズ<span class="opt">${day}（土曜）1回${venue}1日 1レース</span></h1><span class="race_name">${raceName}</span><div class="cell category">${category}</div><div class="cell course">コース：1,600メートル（${course}）</div><div>発走時刻：<strong>10時05分</strong></div><div class="refresh_line"><div class="cell time"><strong>${phase}</strong></div></div>`;
 const td=(n,q)=>`<tr><th scope="row">${n}</th><td>${q}</td></tr>`;
 const table=(cap,rows)=>`<table class="basic narrow-xy ${classes[page]}"><caption>${cap}</caption><tbody>${rows}</tbody></table>`;
 const range=page==='wide'?'<span class="min">2.0</span><span class="cap">-</span><span class="max">3.0</span>':value;
 if(page==='win_place')body+=table('合成単複',entrants.map(h=>`<tr><td class="waku"><img alt="枠${frames[h]}" /></td><td class="num">${h}</td><td class="odds_tan">${value}</td><td class="odds_fuku"><span>1.0</span>-<span>2.0</span></td></tr>`).join(''));
 else if(page==='trifecta')for(const a of entrants)for(const b of entrants.filter(x=>x!==a)){
  body+=`<li><div class="cap"><span>1着</span></div><div class="num">${a}</div><div class="cap"><span>2着</span></div><div class="num">${b}</div>`+
   table('3着',entrants.filter(x=>x!==a).filter(c=>[a,b,c].join('-')!==omit).map(c=>td(c,c===b?'&nbsp;':value)).join(''))+'</li>';
 }else if(page==='trio')for(let i=0;i<entrants.length;i++)for(let j=i+1;j<entrants.length-1;j++)body+=table(`${entrants[i]}-${entrants[j]}`,
  entrants.slice(j+1).filter(c=>[entrants[i],entrants[j],c].join('-')!==omit).map(c=>td(c,value)).join(''));
 else if(page==='frame_quinella')for(const f of [1,2,3])body+=table(`<img alt="枠${f}" />`,[1,2,3].filter(g=>g>=f).map(g=>td(g,f===g&&f!==3?'&nbsp;':value)).join(''));
 else for(const a of entrants)body+=table(String(a),entrants.filter(b=>page==='exacta'||b>a).filter(b=>[a,b].join('-')!==omit).map(b=>td(b,b===a?'&nbsp;':range)).join(''));
 return body;
}
