/** @typedef {{id:number,name:string,role:'leader'|'follower',term:number,log_position:number,available:boolean,fenced:boolean,vote_enabled:boolean}} Node */
/** @typedef {{node_id:number,name:string,current_log_position:number|null,role:string|null,available:boolean|null,caught_up:boolean,log_position_at_activation:number|null,caught_up_at:string|null}} ConfigurationMember */
/** @typedef {{id:number,generation:number|null,status:'preparing'|'active'|'superseded'|'cancelled',base_config_id:number|null,target_log_position:number,target_primary_name:string,initiated_by:string,reason:string,request_id:string|null,blocked_reason:string|null,initiated_at:string,activated_at:string|null,members:ConfigurationMember[],catch_up:{target_log_position:number,caught:number,total:number}}} Configuration */
/** @typedef {{id:number,name:string,fail_primary:boolean,fence_primary:boolean,max_data_loss:number,required_term:number}} Scenario */
/** @typedef {{id:number,scenario_id:number,label:string,status:string,snapshot:object,result:object|null}} Drill */
export {};
