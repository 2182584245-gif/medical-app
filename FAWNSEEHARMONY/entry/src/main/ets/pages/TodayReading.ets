import {cloud} from '../services/Cloud';
import {PAPER,INK,MUTED,TEAL,WHITE,PageResult,TMHeader,TMButton,TMNote,TMLabel,TMField,operationId} from './TodayShared';
interface Inline {tag:string;text?:string;children?:Inline[];}
interface Cell {children:Inline[];}
interface TableRow {cells:Cell[];}
interface Block {tag:string;children?:Inline[];items?:Inline[][];rows?:TableRow[];blocks?:Block[];asset_id?:string;alt?:string;caption?:string;}
interface Article {id:string;title:string;source_title:string;summary:string;blocks:Block[];cover_asset?:string;}
interface Picture {content:string;media_type:string;}
interface Social {liked:boolean;saved:boolean;following:boolean;read:boolean;likes:number;saves:number;comments:number;}
interface Comment {id:string;content:string;display_name?:string;author_name?:string;likes:number;liked:boolean;replies:number;}
interface Bookmark {article_id:string;title:string;}
interface Version {version:string;data_generation:string;}
interface Receipt {status:string;}
class Paragraph { 
  text:string;
  heading:boolean;
  pic:string;
  constructor(text:string, heading:boolean=false, pic:string=''){ this.text=text; this.heading=heading; this.pic=pic; } }
function inlineText(ns:Inline[]):string{return ns.map((n:Inline)=>n.tag==='br'?'\n':n.text||inlineText(n.children||[])).join('');}
function paragraphs(bs:Block[]):Paragraph[]{
  let result:Paragraph[]=[];
  for(const b of bs){
    if(b.blocks){result=result.concat(paragraphs(b.blocks));continue;}
    const body=b.items?b.items.map((v:Inline[],i:number)=>(b.tag==='ol'?(i+1)+'. ':'• ')+inlineText(v)).join('\n'):b.rows?b.rows.map((r:TableRow)=>r.cells.map((c:Cell)=>inlineText(c.children)).join('　')).join('\n'):inlineText(b.children||[]);
    result.push(new Paragraph(body||b.caption||b.alt||'',/^h[1-6]$/.test(b.tag),b.asset_id||''));
  }
  return result;
}
@Component
struct ArticleImage {
  assetId:string='';
  @State src:string='';
  @State failed:boolean=false;
  private alive:boolean=false;
  aboutToAppear(){this.alive=true;this.load();}
  aboutToDisappear(){this.alive=false;}
  private async load():Promise<void>{try{const a=await cloud<Picture>('/v1/member/reading/assets/'+encodeURIComponent(this.assetId));if(this.alive)this.src='data:'+a.media_type+';base64,'+a.content;}catch{if(this.alive)this.failed=true;}}
  build(){Column(){if(this.src){Image(this.src).width('100%').constraintSize({maxHeight:450}).objectFit(ImageFit.Contain)}else if(this.failed){Text('图片未载入 · 重试').fontColor(MUTED).onClick(()=>this.load())}}.width('100%')}
}
@Component
export struct TodayReading{
  @Prop @Watch('refresh') route:string='';
  onNavigate:(r:string)=>void=()=>{};
  @State article:Article|null=null;
  @State segments:Paragraph[]=[];
  @State social:Social|null=null;
  @State comments:Comment[]=[];
  @State total:number=0;
  @State replyParent:string='';
  @State draft:string='';
  @State bookmarks:Bookmark[]=[];
  @State showSaved:boolean=false;
  @State busy:boolean=false;
  @State error:string='';
  private serial:number=0;
  private retryKey:string='';
  private operation:string='';
  private version:string='';
  private generation:string='';
  private getArticleId():string{return decodeURIComponent(this.route.split('/')[2]||'');}
  aboutToAppear(){this.refresh();}
  aboutToDisappear(){this.serial++;}
  refresh():void{this.replyParent='';this.draft='';this.showSaved=false;this.article=null;this.segments=[];this.social=null;this.comments=[];this.retryKey='';this.load(++this.serial);}
  private async commentsPage(offset:number):Promise<void>{
    const id=this.serial;
    const p=await cloud<PageResult<Comment>>('/v1/member/reading/comments?article_id='+encodeURIComponent(this.getArticleId())+'&offset='+offset+(this.replyParent?'&parent_id='+encodeURIComponent(this.replyParent):''));
    if(id!==this.serial)return;this.total=p.total;this.comments=offset?this.comments.concat(p.items):p.items;
  }
  private async load(id:number):Promise<void>{this.error='';try{
    const a=await cloud<Article>('/v1/member/reading/articles/'+encodeURIComponent(this.getArticleId()));if(id!==this.serial)return;this.article=a;this.segments=paragraphs(a.blocks);
    const s=await cloud<Social>('/v1/member/reading/social/'+encodeURIComponent(this.getArticleId()));if(id!==this.serial)return;this.social=s;await this.commentsPage(0);
  }catch(e){if(id===this.serial)this.error=(e as Error).message;}}
  private async mutate(action:string,payload:Object):Promise<void>{
    if(this.busy)return;const token=this.serial;this.busy=true;this.error='';
    try{
      const key=action+JSON.stringify(payload);
      if(key!==this.retryKey){
        const account=await cloud<Version>('/v1/member/local-sync/account-state');
        const version=await cloud<Version>('/v1/member/local-sync/state?resource=reading_social&data_generation='+encodeURIComponent(account.data_generation));
        if(token!==this.serial)return;
        this.operation=operationId();this.version=version.version;this.generation=account.data_generation;this.retryKey=key;
      }
      const outcome=await cloud<Receipt>('/v1/member/local-sync/operations','POST',{'operation_id':this.operation,'action':action,'payload':payload,'base_version':this.version,'data_generation':this.generation} as Record<string, Object|null>,this.operation);if(token!==this.serial)return;this.retryKey='';
      if(outcome.status!=='applied'){await this.load(token);throw Error('内容已在其他设备更新，请核对后再试。');}
      if(action==='reading.comment.add')this.draft='';
      await this.load(token);
    }catch(e){if(token===this.serial)this.error=(e as Error).message;}finally{this.busy=false;}
  }
  private stateAction(kind:string,value:boolean):void{this.mutate('reading.social.set',{'article_id':this.getArticleId(),'kind':kind,'desired':value} as Record<string, Object|null>);}
  private async next():Promise<void>{const token=this.serial;try{const p=await cloud<PageResult<Article>>('/v1/member/reading/recommendations?limit=1&exclude_article_id='+encodeURIComponent(this.getArticleId()));if(token===this.serial&&p.items.length)this.onNavigate('today/reading/'+encodeURIComponent(p.items[0].id));}catch(e){if(token===this.serial)this.error=(e as Error).message;}}
  private async openSaved():Promise<void>{const token=this.serial;try{let offset=0;let rows:Bookmark[]=[];while(true){const p=await cloud<PageResult<Bookmark>>('/v1/member/reading/bookmarks?limit=100&offset='+offset);if(token!==this.serial)return;rows=rows.concat(p.items);offset+=p.items.length;if(!p.items.length||offset>=p.total)break;}this.bookmarks=rows;this.showSaved=true;}catch(e){if(token===this.serial)this.error=(e as Error).message;}}
  build(){
    Column(){
      TMHeader({title:this.showSaved?'我的收藏':'每日一读',back:()=>{if(this.showSaved)this.showSaved=false;else this.onNavigate('today');}})
      Scroll(){Column({space:14}){
        if(this.showSaved){
          ForEach(this.bookmarks,(a:Bookmark)=>{TMButton({label:a.title,action:()=>this.onNavigate('today/reading/'+encodeURIComponent(a.article_id))})},(a:Bookmark)=>a.article_id)
          if(!this.bookmarks.length){TMNote({label:'还没有收藏文章'})}
        }else if(this.article){
          Text(this.article.title).fontSize(27).fontColor(INK).fontWeight(FontWeight.Bold)
          TMNote({label:this.article.source_title})
          if(this.social){Text(this.social.following?'已关注':'关注').fontColor(TEAL).onClick(()=>this.stateAction('follow',!this.social?.following))}
          if(this.article.summary){TMNote({label:this.article.summary})}
          ForEach(this.segments,(p:Paragraph)=>{Column({space:6}){if(p.pic){ArticleImage({assetId:p.pic})} if(p.text){Text(p.text).fontSize(p.heading?23:20).fontColor(INK).lineHeight(p.heading?33:31).fontWeight(p.heading?FontWeight.Bold:FontWeight.Normal).width('100%')}}.width('100%')},(p:Paragraph,index:number)=>String(index))
          if(this.social){
            Row({space:8}){
              Text((this.social.liked?'已点赞':'点赞')+' '+this.social.likes).onClick(()=>this.stateAction('like',!this.social?.liked))
              Text(this.social.saved?'已收藏':'收藏').onClick(()=>this.stateAction('bookmark',!this.social?.saved))
              Text(this.social.read?'已读':'标记已读').onClick(()=>this.stateAction('read',!this.social?.read))
            }.width('100%').justifyContent(FlexAlign.SpaceAround).padding(14).backgroundColor(WHITE).borderRadius(16).enabled(!this.busy)
          }
          TMButton({label:'换一篇',action:()=>this.next()});TMButton({label:'我的收藏',action:()=>this.openSaved()})
          TMLabel({label:this.replyParent?'评论回复':'评论'})
          if(this.replyParent){Text('返回全部评论').fontColor(TEAL).onClick(()=>{this.replyParent='';this.commentsPage(0).catch((e:Error)=>this.error=e.message);})}
          ForEach(this.comments,(c:Comment)=>{
            Column({space:5}){TMNote({label:c.display_name||c.author_name||''});Text(c.content).fontSize(18).fontColor(INK);Row({space:10}){
              Text((c.liked?'已赞 ':'赞 ')+c.likes).fontColor(TEAL).onClick(()=>this.mutate('reading.comment.like',{'article_id':this.getArticleId(),'comment_id':c.id,'desired':!c.liked} as Record<string, Object|null>))
              if(!this.replyParent){Text('回复 '+c.replies).fontColor(TEAL).onClick(()=>{this.replyParent=c.id;this.commentsPage(0).catch((e:Error)=>this.error=e.message);})}
            }}.width('100%').alignItems(HorizontalAlign.Start).padding(12).backgroundColor(WHITE).borderRadius(14)
          },(c:Comment)=>c.id+c.likes+c.replies)
          if(this.comments.length<this.total){TMButton({label:'加载更多评论',action:()=>{this.commentsPage(this.comments.length).catch((e:Error)=>this.error=e.message);}})}
          TMField({label:this.replyParent?'回复评论':'写评论',value:this.draft,change:(v:string)=>this.draft=v,multiline:true})
          TMButton({label:'发布',primary:true,blocked:this.busy||!this.draft.trim(),action:()=>this.mutate('reading.comment.add',{'article_id':this.getArticleId(),'content':this.draft.trim(),'parent_id':this.replyParent||null} as Record<string, Object|null>)})
        }
        if(this.error){TMNote({label:this.error});TMButton({label:'重新读取',action:()=>this.load(this.serial)})}
      }.padding(16).width('100%').alignItems(HorizontalAlign.Start)}.layoutWeight(1).scrollBar(BarState.Off)
    }.width('100%').height('100%').backgroundColor(PAPER)
  }
}
