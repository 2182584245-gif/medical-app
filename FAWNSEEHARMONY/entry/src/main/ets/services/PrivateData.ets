import { common } from '@kit.AbilityKit'
import { preferences,relationalStore } from '@kit.ArkData'
import { clearRecordSnapshots } from './RecordsCacheStore'
let host:common.UIAbilityContext|undefined
export function attachPrivateStorage(context:common.UIAbilityContext):void {host=context}
export async function clearMemberRecordCache(owner:number):Promise<void>{
  if(host&&owner>0)await clearRecordSnapshots(host,owner)
}
export async function removeMemberDrafts(owner:number):Promise<void>{
  if(!host||owner<0)return
  const prefs=preferences.getPreferencesSync(host,{name:'assistant_decisions_'+owner})
  await prefs.clear()
  await prefs.flush()
  if(owner===0)return
  await clearMemberRecordCache(owner)
  const store=await relationalStore.getRdbStore(host,{name:'RecordDrafts.db',securityLevel:relationalStore.SecurityLevel.S2,encrypt:true})
  const rows=await store.querySql("SELECT name FROM sqlite_master WHERE type='table' AND name='drafts'")
  let exists=false
  try{exists=rows.goToNextRow()}finally{rows.close()}
  if(exists)await store.executeSql('DELETE FROM drafts WHERE owner=?',[owner])
}
