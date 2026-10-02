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
