export const race='jra:20000101:5:1',entrants=[1,2,10,11],frames={1:1,2:2,10:3,11:3};
export const roster={entrants,frames};
export const resultTarget={sport:'jra',race_id:race,kind:'result',form:true,url:'https://www.jra.go.jp/JRADB/accessS.html',
 body:new URLSearchParams({cname:'pw01sde1005200001010120000101/AA'}).toString()};
export function jraResult({special=false,tie=false,missing=false,missingWide=false}={}){
 const labels={'単勝':['1'],'複勝':['1','2','10'],'枠連':['1-2'],'馬連':['1-2'],'ワイド':['1-2','1-10','2-10'],'馬単':['1-2'],'3連複':['1-2-10'],'3連単':['1-2-10']};
 const ranks=entrants.map((h,i)=>`<tr><td class="place">${i===3?'取消':tie&&i===1?1:i+1}</td><td class="num">${h}</td></tr>`).join('');
 const payments=Object.entries(labels).filter(([m])=>!missing||m!=='3連単').map(([m,cs])=>`<dl><dt>${m}</dt><dd>${cs.filter(c=>!missingWide||m!=='ワイド'||c!=='2-10').map(c=>
  `<div class="line"><div class="num">${special&&m==='3連単'?'特払':c}</div><div class="yen">${special&&m==='3連単'?'70':'1,230'}<span class="unit">円</span></div><div class="pop">1番人気</div></div>`).join('')}</dd></dl>`).join('');
 return jraBody('win_place').split('<div class="refresh_line">')[0]+`<table><thead><tr><th class="place">着順</th><th class="num">馬<br>番</th></tr></thead><tbody>${ranks}</tbody></table>`+
  `<div class="refund_area"><h2>払戻金</h2><div class="refund_unit mt15">${payments}</div><div class="caution">一般説明: 特払い</div></div>`+
  `<a href="/JRADB/accessS.html?CNAME=pw01sde1005200001010120000101/AA">合成結果1</a>`+
  `<a href="/JRADB/accessS.html?CNAME=pw01sde1005200001010220000101/BB">合成結果2</a>`;
}
export const catalogTarget={sport:'jra',race_id:'jra:20000101:0:0',kind:'schedule',discovery_stage:'catalog',form:true,
 url:'https://www.jra.go.jp/JRADB/accessO.html',body:'cname=pw15oli00%2F6D'};
export const venueNavigation='pw15orl00052000010120000101/AA';
const link=(name,label)=>`<a href="#" onclick="return doAction('/JRADB/accessO.html', '${name}');">${label}</a>`;
export const jraCatalog=`<h3>1月1日</h3>${link(venueNavigation,'1回東京1日')}<h3>1月2日</h3>${link('pw15orl00052000010220000102/BB','1回東京2日')}`;
export const resultCatalogTarget={...catalogTarget,program_kind:'results',url:'https://www.jra.go.jp/JRADB/accessS.html',body:'cname=pw01sli00%2FAF'};
export const resultVenueNavigation='pw01srl10052000010120000101/AA';
export const jraResultCatalog=`<h1>レース結果 開催選択</h1><h3>1月1日</h3><a href="#" onclick="return doAction('/JRADB/accessS.html', '${resultVenueNavigation}');">1回東京1日</a>`;
export function jraResultProgram({name='pw01sde1005200001010120000101/AA',empty=false}={}){
 return `<h1>レース結果 レース選択 2000年1月1日 1回東京1日</h1><table><tr><th class="race_num">レース結果</th><th class="odds">最終オッズ</th></tr>`+
  (empty?'':`<tr><th class="race_num"><a href="/JRADB/accessS.html?CNAME=${name}"><img alt="1レース" /></a></th><td class="odds">${link('pw151ou1005200001010120000101Z/BB','1レースオッズ')}</td></tr>`)+`</table>`;
}
export function jraProgram({clock='10:05',no=1,day='20000101',venue=5,navigationMode='intermediate'}={}){
 const prefixes={win_place:'pw151ou',frame_quinella:'pw153ou',quinella:'pw154ou',wide:'pw155ou',exacta:'pw156ou',trio:'pw157ou',trifecta:'pw158ou'};
 const names=Object.entries(prefixes).map(([page,prefix])=>link(`${prefix}${navigationMode==='final'?'10':'S3'}${String(venue).padStart(2,'0')}20000101${String(no).padStart(2,'0')}${day}Z${page==='trio'?'99':''}/AA`,page));
 return `<h1>2000年1月1日 1回東京1日</h1><table><tr><th>${names[0]}</th><td class="time">${clock}</td><td>${names.join('')}</td></tr></table>`;
}
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
