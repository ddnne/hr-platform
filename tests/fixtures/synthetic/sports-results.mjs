// Independent fabricated results; no downloaded participants, odds, payouts or identifiers.
const markets={rtw:[1,2],rfw:[1,2],rt3:[1,2,3],rf3:[1,2,3],wid:[1,3],tns:[1],fns:[2]};
export function autoResult(pending=false){
 const refundInfo=Object.fromEntries(Object.entries(markets).map(([key,ids])=>[key,{typeCode:0,typeName:'synthetic normal',list:pending?[]:[{
  ...(ids.length===1?{carNo:ids[0]}:Object.fromEntries(ids.map((id,i)=>[`${i+1}thCarNo`,id]))),refund:'1,230',pop:1,refundVotes:1}]}]));
 return JSON.stringify({result:'Success',body:{raceResult:pending?[]:[{carNo:1,order:1,accidentName:null},{carNo:2,order:1,accidentName:null},{carNo:3,order:'失格',accidentName:'synthetic accident'}],refundInfo}});
}
export function keirinResult(pending=false){
 const fields={WH2:'4=4',WT2:'4-4',SH2:'1=2',ST2:'1-2',RH3:'1=2=3',RT3:'1-2-3',W:'1=3'};
 return JSON.stringify({resultCd:0,haraiGakuDispFlg:!pending,tyakujyunDispFlg:!pending,lastUpdateTime:'synthetic provider display',
  tyakujyunItemSubData:[{syaban:'1',tyaku:'1'},{syaban:'2',tyaku:'2'}],haraiGakuSubData:{APartReturnDispFlg:true,
   ...Object.fromEntries(Object.entries(fields).map(([k,combination])=>[k+'HaraiGakuDispItemSubData',[{kumiBan:combination,haraiGaku:'1230',kumiDispFlg:true,ninkiDispFlg:true}]]))}});
}
export const boatResult='<table><thead><tr><th>着</th><th>枠</th></tr></thead><tbody><tr><td>１</td><td>1</td></tr><tr><td>Ｆ</td><td>2</td></tr></tbody></table>'+
 '<table>'+[['3連単','1 - 2 - 3'],['3連複','1 = 2 = 3'],['2連単','1 - 2'],['2連複','1 = 2'],['拡連複','1 = 3'],['単勝','1'],['複勝','2']]
 .map(([label,combination])=>`<tr><td rowspan="2">${label}</td><td>${combination}</td><td><span class="is-payout1">&yen;1,230</span></td></tr><tr><td>&nbsp;</td><td><span class="is-payout1">&nbsp;</span></td></tr>`).join('')+'</table>'+
 '<table><thead><tr><th>返還</th></tr></thead><tbody><tr><td>2</td></tr></tbody></table>';
