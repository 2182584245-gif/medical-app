import {cloud} from '../services/Cloud';
import {INK,WHITE,TEAL,TMLabel,TMNote,TMButton,operationId} from './TodayShared';
interface ArchiveCategory {domain:string;title:string;text:string;revision:number;status:string;max_chars:number;}
interface Snapshot {categories:ArchiveCategory[];initialization_pending:boolean;}
interface Change {domain:string;text:string;expected_revision:number;}
const ROOT='/v1/member/memory/ai-profile/categories';
@Component
export struct MineArchive{
  @State rows:ArchiveCategory[]=[];
  @State drafts:Map<string,string>=new Map();
  @State error:string='';
  @State busy:boolean=false;
  private ticket:number=0;
  private timer:number=-1;
  private lastKey:string='';
  private pendingOp:string='';
  aboutToAppear(){this.load();}
  aboutToDisappear(){this.ticket++;clearTimeout(this.timer);}
  private async load():Promise<void>{const t=++this.ticket;clearTimeout(this.timer);try{
    const p=await cloud<Snapshot>(ROOT);if(t!==this.ticket)return;
    this.rows=p.categories;this.error='';
    if(p.initialization_pending)this.timer=setTimeout(()=>this.load(),5000);
  }catch(e){if(t===this.ticket)this.error=(e as Error).message;}}
  private setDraft(row:ArchiveCategory,v:string):void{const d=new Map(this.drafts);if(v===row.text)d.delete(row.domain);else d.set(row.domain,v);this.drafts=d;}
  private async save():Promise<void>{
    if(this.busy||!this.drafts.size)return;
    const changes:Change[]=[];this.rows.forEach((r:ArchiveCategory)=>{const text=this.drafts.get(r.domain);if(text!==undefined)changes.push({domain:r.domain,text:text,expected_revision:r.revision});});
    const key=JSON.stringify(changes);if(this.lastKey!==key){this.lastKey=key;this.pendingOp=operationId();}
    const t=++this.ticket;clearTimeout(this.timer);this.busy=true;
    try{const p=await cloud<Snapshot>(ROOT,'PUT',{'categories':changes,'request_id':this.pendingOp} as Record<string, Object|null>,this.pendingOp);if(t!==this.ticket)return;
      if(changes.some((c:Change)=>!p.categories.some((r:ArchiveCategory)=>c.domain===r.domain&&c.text===r.text)))throw Error('保存结果尚未确认。');
      this.rows=p.categories;this.drafts=new Map();this.lastKey='';this.error='';this.getUIContext().getPromptAction().showToast({message:'生活档案已保存'});
    }catch(e){if(t===this.ticket)this.error=(e as Error).message;}finally{this.busy=false;}
  }
  private async regenerate(r:ArchiveCategory):Promise<void>{
    if(this.busy)return;if(this.drafts.has(r.domain)){this.error='请先保存此类尚未保存的修改。';return;}
    const t=++this.ticket;clearTimeout(this.timer);this.busy=true;
    try{const op=operationId();await cloud<Object>(ROOT+'/'+encodeURIComponent(r.domain)+'/refresh','POST',{'expected_revision':r.revision,'request_id':op} as Record<string, Object|null>,op);if(t===this.ticket)await this.load();}
    catch(e){if(t===this.ticket)this.error=(e as Error).message;}finally{this.busy=false;}
  }
  build(){Column({space:12}){
    TMLabel({label:'我的AI生活档案'})
    ForEach(this.rows,(r:ArchiveCategory)=>{
      Column({space:8}){TMLabel({label:r.title})
        TextArea({text:this.drafts.get(r.domain)??r.text,placeholder:'暂无已保存的资料'}).fontSize(18).minLines(3).maxLength(r.max_chars).onChange((v:string)=>this.setDraft(r,v))
        Text(r.status==='pending'?'整理中…':'重新总结').fontSize(16).fontColor(TEAL).onClick(()=>this.regenerate(r))
      }.width('100%').padding(12).backgroundColor(WHITE).borderRadius(18)
    },(r:ArchiveCategory)=>r.domain+r.revision+r.status)
    TMButton({label:'保存生活档案',primary:true,blocked:this.busy||!this.drafts.size,action:()=>this.save()})
    if(this.error){TMNote({label:this.error});TMButton({label:'重新读取',action:()=>this.load()})}
  }.width('100%')}
}
