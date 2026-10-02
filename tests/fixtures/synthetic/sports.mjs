// Fabricated public protocol shapes. No downloaded odds or participant names.
export const ids=[1,2,3,4,5,6];
function syntheticCombinations(ids,n,ordered){
 const tuples=n===1?ids.map(a=>[a]):n===2?ids.flatMap(a=>ids.map(b=>[a,b])):ids.flatMap(a=>ids.flatMap(b=>ids.map(c=>[a,b,c])));
 return tuples.filter(c=>new Set(c).size===n&&(ordered||c.every((v,i)=>i===0||c[i-1]<v)));
}
export function autoBody(price='4.0',phase=0){
 const fields={rtwOddsList:[2,true],rfwOddsList:[2,false],rt3OddsList:[3,true],rf3OddsList:[3,false],widOddsList:[2,false],tnsOddsList:[1,false],fnsOddsList:[1,false]};
 const body={statusCode:phase,playerList:ids.map(carNo=>({carNo})),salesInfo:{updateDate:'2000-01-02 00:09:25'}};
 for(const [name,[n,ordered]] of Object.entries(fields)) {const data={};for(const c of syntheticCombinations(ids,n,ordered)) {
  let at=data;for(const id of c.slice(0,-1))at=at[id]??={};at[c.at(-1)]=['widOddsList','fnsOddsList'].includes(name)?{min:price,max:'9.0'}:price;
}body[name]=data;}return JSON.stringify({result:'Success',body});
}
export const autoCatalog=JSON.stringify({result:'Success',body:{date:'2000-01-01 00:05:01',today:[{placeCode:6,oddsRaceNo:7,cancelFlg:'0'}],tomorrow:[{placeCode:2}]}});
export const autoProgram=JSON.stringify({result:'Success',body:{placeCode:6,raceNo:7,finalRaceNo:8,raceStartTime:'24:05',telvoteTime:'24:02'}});
export const boatCatalog='<a href="/owpc/pc/race/raceindex?jcd=02&amp;hd=20000101">synthetic</a><a href="/owpc/pc/race/raceindex?jcd=05&amp;hd=19991231">old</a>';
export const boatProgram='<table>'+[1,2,3].map(no=>`<tr><td><a href="/owpc/pc/race/odds3t?jcd=02&amp;hd=20000101&amp;rno=${no}">${no}R</a></td><td>10:${String(no*10).padStart(2,'0')}</td><td>synthetic</td></tr>`).join('')+'</table>';
export function boatOddsBody(markets){
 const specs={trifecta:['3連単オッズ',3,true],trio:['3連複オッズ',3,false],exacta:['2連単オッズ',2,true],quinella:['2連複オッズ',2,false],wide:['拡連複オッズ',2,false],win:['単勝オッズ',1,false],place:['複勝オッズ',1,false]};
 return '<p class="tab4_time">オッズ更新時間 09:00</p>'+markets.map(name=>{
  const [label,size,ordered]=specs[name],tuples=syntheticCombinations(ids,size,ordered);
  if(size===1)return `<span>${label}</span><table><tbody>`+ids.map(id=>`<tr><td>${id}</td><td class="oddsPoint">4.0</td></tr>`).join('')+'</tbody></table>';
  const groups=ids.map(id=>tuples.filter(c=>c[0]===id)),height=Math.max(...groups.map(g=>g.length));
  const head=ids.map(id=>`<th>${id}</th><th colspan="${size-1}">synthetic</th>`).join('');
  const body=Array.from({length:height},(_,i)=>'<tr>'+groups.map(g=>g[i]?g[i].slice(1).map(id=>`<td>${id}</td>`).join('')+'<td class="oddsPoint">4.0</td>':'<td></td>'.repeat(size)).join('')+'</tr>').join('');
  return `<span>${label}</span><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
 }).join('');
}
export const keirinCatalog=JSON.stringify({resultCd:0,RaceList:[{kaisaiDate:'20000101',naibuKeirinCd:'47',raceNum:'0',touhyouLivePara:'synthetic-public-navigation',tyusiKbn:'synthetic-cancel-label'}]});
export const keirinIdentity=JSON.stringify({resultCd:0,data:{kaisaiDate:'20000101',keirinJyoCd:'47',raceNo:'2'}});
export const keirinProgram='<script>jsonData["PC0201"] = '+JSON.stringify({resultCd:0,C0201data:{selKaisai:'20000101',selKjyoCd:'47',selRaceNo:'2',cntRace:0,
 C0201racedtl:{aftStartTime:'17:05',aftBetTime:'17:02',bfrStartTime:'17:00',bfrBetTime:'16:57'},
 C0201race:[1,2,3].map(i=>({encParaR:'synthetic-public-navigation-'+i,flgRaceEnd:i===1?'1':'0'}))}})+';</script>';

// Fabricated runner support, separate from odds and result displays.
export const keirinRunners=JSON.stringify({resultCd:0,data:{syaCnt:'6',wakuKbn:'synthetic category',
 sensyuInfoList:Array.from({length:6},(_,i)=>({syaban:String(i+1),wakuBan:String(i+1),kesyaFlg:i===2?'synthetic withdrawal':'0',sensyuSei4Char:'synthetic unused name'}))}});
