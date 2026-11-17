import {api} from './client.js';
export const nodesAPI={list:()=>api('/nodes'),save:(data,id)=>api('/nodes'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(data)})};
export const scenariosAPI={list:()=>api('/scenarios'),create:data=>api('/scenarios',{method:'POST',body:JSON.stringify(data)})};
export const drillsAPI={list:()=>api('/drills'),get:id=>api('/drills/'+id),create:data=>api('/drills',{method:'POST',body:JSON.stringify(data)})};
export const configurationsAPI={list:()=>api('/configurations'),create:data=>api('/configurations',{method:'POST',body:JSON.stringify(data)}),activate:id=>api('/configurations/'+id+'/activate',{method:'POST',body:'{}'}),cancel:id=>api('/configurations/'+id+'/cancel',{method:'POST',body:'{}'})};
