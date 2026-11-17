import {nodesAPI,scenariosAPI,drillsAPI,configurationsAPI} from '../api/entities.js';
export const store={nodes:[],scenarios:[],drills:[],configurations:[],async refresh(){[this.nodes,this.scenarios,this.drills,this.configurations]=await Promise.all([nodesAPI.list(),scenariosAPI.list(),drillsAPI.list(),configurationsAPI.list()]);}};
