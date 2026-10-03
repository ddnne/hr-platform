/** Shared validation for provider program dates and advertised clocks. */
import config from '../../configs/sports-collection.json';
export function date(value:unknown):string {
 const s=String(value).replaceAll('-','');if(!/^\d{8}$/.test(s))throw new Error('PROGRAM_DATE');
 const iso=`${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`;
 const milliseconds=Date.parse(iso+'T00:00:00Z');
 if(!Number.isFinite(milliseconds)||new Date(milliseconds).toISOString().slice(0,10)!==iso)throw new Error('PROGRAM_DATE');
 return s;
}
export function programClock(day:string,label:unknown):string|null {
 const d=date(day);if(label===null||label===undefined||label==='')return null;
 const m=String(label).match(/^(\d{1,2}):(\d{2})$/);if(!m)throw new Error('PROGRAM_CLOCK');
 const hour=Number(m[1]),minute=Number(m[2]);if(hour>config.discovery.maximum_clock_hour||minute>=60)throw new Error('PROGRAM_CLOCK');
 const start=Date.parse(`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}T00:00:00Z`)-config.discovery.clock_timezone_offset_minutes*60_000;
 return new Date(start+(hour*60+minute)*60_000).toISOString();
}
