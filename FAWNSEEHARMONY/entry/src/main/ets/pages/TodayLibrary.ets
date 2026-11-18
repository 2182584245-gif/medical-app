import {actorId,rpc} from '../services/Cloud';
import {CATALOG,CatalogItem,CatalogSection} from './TodayCatalog';
import {PAPER,INK,MUTED,WHITE,TEAL,Choice,operationId,TMButton,TMHeader,TMBanner,TMPills,TMLabel,TMNote} from './TodayShared';
interface FavoriteList { schema_version:number; foods:string[]; recipes:string[]; }
interface Prefs {diet_library_v1?:FavoriteList;}
@Component
export struct TodayLibrary {
  @Prop @Watch('refresh') route:string='';
  onNavigate:(route:string)=>void=()=>{};
  @State category:string='全部';
  @State search:string='';
  @State favorites:string[]=[];
  @State error:string='';
  @State busy:boolean=false;
  private serial:number=0;
  private getKind():string{return this.route.split('/')[2];}
  private getTitle():string{return this.getKind()==='foods'?'食物资料':'食谱推荐';}
  private getItem():CatalogItem|undefined{return CATALOG.find((i:CatalogItem)=>i.kind===this.getKind()&&i.id===this.route.split('/')[3]);}
  private getBase():string{return 'today/library/'+this.getKind();}
  aboutToAppear(){this.refresh();}
  aboutToDisappear(){this.serial++;}
  refresh():void{this.search='';this.category='全部';this.load(++this.serial);}
  private async load(id:number):Promise<void>{try{const p=await rpc<Prefs>('preferences','get_preferences',[actorId()]);if(this.serial===id){this.favorites=(this.getKind()==='foods'?p.diet_library_v1?.foods:p.diet_library_v1?.recipes)||[];this.error='';}}catch(e){if(id===this.serial)this.error=(e as Error).message;}}
  private async toggle(item:CatalogItem):Promise<void>{
    if(this.busy)return;const id=this.serial;this.busy=true;
    try{
      const p=await rpc<Prefs>('preferences','get_preferences',[actorId()]);
      if(this.serial!==id)return;
      const v:FavoriteList=p.diet_library_v1||{schema_version:1,foods:[],recipes:[]};
      let ids=item.kind==='foods'?v.foods:v.recipes;ids=ids.includes(item.id)?ids.filter(k=>k!==item.id):ids.concat(item.id);
      if(ids.length>100)throw Error('已收藏100份资料，请先取消一项收藏。');
      if(item.kind==='foods')v.foods=ids;else v.recipes=ids;
      const saved=await rpc<Prefs>('preferences','update_preferences',[actorId(),{'diet_library_v1':v} as Record<string, Object|null>],undefined,operationId());
      if(this.serial!==id)return;
      if(!saved.diet_library_v1)throw Error('保存结果尚未确认，请重新读取。');
      this.favorites=item.kind==='foods'?saved.diet_library_v1.foods:saved.diet_library_v1.recipes;this.error='';
    }catch(e){if(this.serial===id)this.error=(e as Error).message;}finally{this.busy=false;}
  }
  private list():CatalogItem[]{return CATALOG.filter((i:CatalogItem)=>i.kind===this.getKind()&&(this.category==='全部'||this.category===i.category||this.category==='收藏'&&this.favorites.includes(i.id))&&(i.name+i.category+i.summary+i.sections.map((s:CatalogSection)=>s.body).join(' ')).includes(this.search.trim()));}
  private filters():Choice[]{return (this.getKind()==='foods'?['全部','蔬菜','水果','谷薯','奶豆','肉蛋鱼','收藏']:['全部','家常热菜','蔬菜豆品','早餐加餐','均衡主餐','收藏']).map((s:string)=>new Choice(s,s));}
  @Builder body(i:CatalogItem){
    Image(i.image).height(174).width('100%').objectFit(ImageFit.Cover).borderRadius(22)
    TMLabel({label:i.name});TMNote({label:i.category+' · '+i.summary})
    TMButton({label:this.favorites.includes(i.id)?'已收藏':'收藏',action:()=>this.toggle(i),blocked:this.busy})
    ForEach(i.sections,(s:CatalogSection)=>{Column({space:8}){TMLabel({label:s.title});TMNote({label:s.body})}.padding(14).width('100%').backgroundColor(WHITE).borderRadius(17)},(s:CatalogSection)=>s.title)
  }
  build(){
    Column(){
      TMHeader({title:this.getTitle(),back:()=>this.onNavigate(this.getItem()?this.getBase():'today')})
      Scroll(){Column({space:12}){
        if(this.getItem()){this.body(this.getItem()!)}else{
          TMBanner({title:this.getTitle(),subtitle:this.getKind()==='foods'?'认识食物，了解日常营养':'把好好吃饭，变成每天的小期待',art:this.getKind()==='foods'?$r('app.media.tm_food_info_banner'):$r('app.media.tm_recipe_banner')})
          TextInput({placeholder:'搜索'+this.getTitle(),text:this.search}).onChange((s:string)=>this.search=s)
          TMPills({options:this.filters(),selected:this.category,change:(s:string)=>this.category=s})
          TMNote({label:'共 '+this.list().length+' 项'})
          ForEach(this.list(),(i:CatalogItem)=>{Row({space:10}){Image(i.image).width(80).height(80).borderRadius(13);Column({space:6}){TMLabel({label:i.name});TMNote({label:i.summary})}.layoutWeight(1);Text('›').fontColor(TEAL)}.width('100%').backgroundColor(WHITE).padding(12).borderRadius(18).onClick(()=>this.onNavigate(this.getBase()+'/'+i.id))},(i:CatalogItem)=>i.id)
        }
        if(this.error){TMNote({label:this.error});TMButton({label:'重新读取',action:()=>this.load(this.serial)})}
      }.width('100%').padding(16)}.layoutWeight(1).scrollBar(BarState.Off)
    }.height('100%').width('100%').backgroundColor(PAPER)
  }
}
