import {preferences} from '@kit.ArkData';
import {common} from '@kit.AbilityKit';
import {todayWeather} from './TodayWeather';
import {actorId, currentMember, Member, rpc, cloud} from '../services/Cloud';
import {TodayHabits, Goal, Reminder} from './TodayHabits';
import {TodayLibrary} from './TodayLibrary';
import {TodayReading} from './TodayReading';
import {TodayGoods} from './TodayGoods';
import {PAPER,INK,TEAL,MUTED,WHITE,DAYS,PageResult,expansion,localTime,dayAt,weekday,TMNote} from './TodayShared';

interface RecordDate { occurred_at?:string; local_date?:string; measured_at?:string; }
interface ArticleKey { id:string; title?:string; }
class Slide {
  
  label:string;
  title:string;
  target:string;
  constructor(label:string, title:string, target:string){ this.label=label; this.title=title; this.target=target; }
}
const DAY_COLORS:string[]=['#F4F1EA','#F6EDE9','#F2ECF7','#ECF1E6','#E8F0F5','#E9F2EA','#F7DEA5'];
const SLIDES: Slide[]=[
  new Slide('每日一读','把日常细碎的感知，妥帖收进慢时光里','reading'),
  new Slide('生活好物','把日常过得更有心，发现生活好物','today/goods'),
  new Slide('食物资料','认识食物，了解日常营养','today/library/foods'),
  new Slide('食谱推荐','把好好吃饭，变成每天的小期待','today/library/recipes')
];
const GALLERIES:Resource[][]=[
  [$r('app.media.tm_reading_01'),$r('app.media.tm_reading_02'),$r('app.media.tm_reading_03')],
  [$r('app.media.tm_goods_01'),$r('app.media.tm_goods_02'),$r('app.media.tm_goods_03')],
  [$r('app.media.tm_food_01'),$r('app.media.tm_food_02'),$r('app.media.tm_food_03')],
  [$r('app.media.tm_recipe_01')]
];
let galleryMember:Member|null=null;
let sessionPicks:number[]=[];
function sessionCovers(context:common.UIAbilityContext):number[]{
  const member=currentMember();
  if(galleryMember===member&&sessionPicks.length===GALLERIES.length)return sessionPicks;
  const store=preferences.getPreferencesSync(context,{name:'today_cover_gallery'});
  const previous=(store.getSync('indices','') as string).split(',');
  const picks=GALLERIES.map((art:Resource[],i:number)=>{
    const candidates=art.map((r:Resource,index:number)=>index).filter((index:number)=>art.length===1||!previous[i]||index!==Number(previous[i]));
    return candidates[Math.floor(Math.random()*candidates.length)];
  });
  store.putSync('indices',picks.join(','));store.flush().catch(()=>{});
  galleryMember=member;sessionPicks=picks;return picks;
}
@Component
export struct TodayPage {
  @Prop @Watch('changed') route:string='today';
  onNavigate:(route:string)=>void=()=>{};
  @State now:Date=new Date();
  @State goals:Goal[]=[];
  @State nextReminder:string='未设提醒';
  @State marked:string[]=[];
  @State error:string='';
  @State slide:number=0;
  @State coverIndices:number[]=[0,0,0,0];
  @State articleTitle:string=SLIDES[0].title;
  private articleId:string='';
  private carousel:SwiperController=new SwiperController();
  @State weather:string='位置暂不可用 · 天气暂不可用';
  private serial:number=0;
  private clock:number=-1;
  aboutToAppear() { this.changed(); this.clock=setInterval(()=>this.now=new Date(),60000); }
  aboutToDisappear() { this.serial++;clearInterval(this.clock); }
  changed():void { const id=++this.serial; if(this.route==='today'){this.load(id);this.coverIndices=sessionCovers(this.getUIContext().getHostContext() as common.UIAbilityContext);this.loadReading(id);todayWeather().then((v:string)=>{if(this.serial===id)this.weather=v;});} }
  private async recent(method:string):Promise<string[]> {
    let offset=0;
    const dates:string[]=[];
    const start=dayAt(-6);
    while(true) {
      const page=await expansion<PageResult<RecordDate>>(method,{'offset':offset,'limit':100} as Record<string, Object|null>);
      let old=false;
      for(const row of page.items){
        const stamp=row.occurred_at||row.measured_at||'';
        const when=new Date(stamp);
        const date=row.local_date || (Number.isFinite(when.getTime())?new Date(when.getTime()+28800000).toISOString().slice(0,10):'');
        if(date>=start && !dates.includes(date))dates.push(date);
        if(date<start)old=true;
      }
      offset+=page.items.length;
      if(old||offset>=page.total||page.items.length===0)break;
    }
    return dates;
  }
  private async load(id:number):Promise<void> {
    this.error='';
    try {
      const results=await Promise.all([expansion<PageResult<Goal>>('list_goals',{'status':'active','limit':100} as Record<string, Object|null>),rpc<Reminder[]>('health','list_reminders',[actorId()]),this.recent('list_records'),this.recent('list_observations')]);
      if(this.serial!==id)return;
      this.goals=results[0].items;this.marked=results[2].concat(results[3]);
      const active=results[1].filter(v=>v.status==='active').sort((a:Reminder,b:Reminder)=>a.scheduled_at.localeCompare(b.scheduled_at));
      this.nextReminder=active.length?localTime(new Date(active[0].scheduled_at)):'未设提醒';
    } catch(e) { if(this.serial===id)this.error=(e as Error).message; }
  }
  private greeting():string {
    const h=this.now.getHours();return h<5?'夜深了，安心休息':(h<9?'早上好':h<11?'上午好':h<14?'中午好':h<16?'下午好':h<19?'傍晚好':'晚上好')+'，照顾好自己';
  }
  private greetingDetail():string {
    const h=this.now.getHours();return h<5?'夜阑人静，给身心一段安稳。':h<9?'晨曦入室，一杯温水开启今天。':h<11?'朝光正好，忙碌间舒展片刻。':h<16?'午光正好，给身心留一点空闲。':h<19?'暮色渐柔，让生活的步调慢下来。':'灯火可亲，整理心情，好好休息。';
  }
  private async loadReading(id:number):Promise<void>{
    try{
      const page=await cloud<PageResult<ArticleKey>>('/v1/member/reading/catalog?scope=today&limit=1');
      if(this.serial===id&&page.items.length){this.articleId=page.items[0].id;this.articleTitle=page.items[0].title||SLIDES[0].title;}
    }catch{ /* Keep the original cover until the catalog can be read. */ }
  }
  private async openSlide(slide:Slide):Promise<void> {
    if(slide.target!=='reading'){ this.onNavigate(slide.target);return; }
    if(this.articleId){this.onNavigate('today/reading/'+encodeURIComponent(this.articleId));return;}
    const id=this.serial;
    try{
      const result=await cloud<PageResult<ArticleKey>>('/v1/member/reading/catalog?scope=today&limit=1');
      if(this.serial!==id)return;
      if(!result.items.length){this.error='暂时没有可阅读的文章。';return;}
      this.onNavigate('today/reading/'+encodeURIComponent(result.items[0].id));
    }catch(e){if(this.serial===id)this.error=(e as Error).message;}
  }
  @Builder header(){
    Row({space:8}){
      Image($r('app.media.records_logo')).width(40).height(40)
      Column({space:2}){
        Text((this.now.getMonth()+1)+'月'+this.now.getDate()+'日 '+localTime(this.now)).fontSize(19).fontWeight(FontWeight.Bold).fontColor(INK)
        Text(this.weather).fontSize(14).fontColor(MUTED).maxLines(1).textOverflow({overflow:TextOverflow.Ellipsis}).onClick(()=>this.getUIContext().getPromptAction().showToast({message:'天气数据：挪威气象局（CC BY 4.0）；网络出口位置：ipwho.is'}))
      }.layoutWeight(1).alignItems(HorizontalAlign.Start)
      Image($r('app.media.tm_icon_basket')).objectFit(ImageFit.Contain).padding(8).width(40).height(44).accessibilityText('生活好物').id('today-open-goods').onClick(()=>this.onNavigate('today/goods'))
      Column(){
        Text(this.nextReminder).fontSize(13).fontColor('#805629').fontWeight(FontWeight.Bold).backgroundColor('#FFF0D3').borderRadius(8).padding({left:4,right:4})
        Image($r('app.media.tm_icon_alarm')).width(23).height(23)
      }.constraintSize({minWidth:44,minHeight:44}).id('today-open-reminders').onClick(()=>this.onNavigate('today/reminders'))
    }.width('100%').height(68).padding({left:15,right:14}).backgroundColor(PAPER)
  }
  @Builder cover(){
    Swiper(this.carousel){
      ForEach(SLIDES,(s:Slide,index:number)=>{
        Stack({alignContent:Alignment.BottomStart}){
          Image(GALLERIES[index][this.coverIndices[index]]).width('100%').height('100%').objectFit(ImageFit.Cover)
          Column({space:4}){
            Text(s.label).fontSize(20).fontWeight(FontWeight.Bold).fontColor('#735A32').backgroundColor('#F7E3A9').borderRadius(18).padding({left:12,right:12,top:5,bottom:5})
            Blank().layoutWeight(1)
            Text(s.target==='reading'?this.articleTitle:s.title).maxLines(2).textOverflow({overflow:TextOverflow.Ellipsis}).fontSize(25).fontWeight(FontWeight.Bold).fontColor(Color.White)
          }.height('100%').width('100%').justifyContent(FlexAlign.End).alignItems(HorizontalAlign.Start).padding(16)
            .linearGradient({angle:180,colors:[['#00000000',0],['#CA142419',1]]})
        }.borderRadius(24).clip(true).onClick(()=>this.openSlide(s))
      },(s:Slide)=>s.target)
    }.height(180).width('100%').autoPlay(true).interval(6000).indicator(false).onChange((i:number)=>this.slide=i)
    Row({space:8}){ ForEach(SLIDES,(s:Slide,i:number)=>{Stack(){Row().width(i===this.slide?8:6).height(i===this.slide?8:6).borderRadius(8).backgroundColor(i===this.slide?TEAL:'#BCC8BF')}.width(16).height(28).accessibilityText(s.label).onClick(()=>this.carousel.changeIndex(i,true))},(s:Slide)=>s.label) }.height(36)
  }
  @Builder week(){
    Column({space:12}){
      Row(){
        Image($r('app.media.tm_icon_leaf')).width(18).height(18).margin({right:7})
        Text('近七天记录').layoutWeight(1).fontWeight(FontWeight.Bold).fontColor(INK).fontSize(25)
        Text('查看详情 ›').fontColor(TEAL).fontSize(17).onClick(()=>this.onNavigate('records'))
      }.width('100%')
      Row({space:4}){
        ForEach(DAYS,(n:number)=>{
          Column({space:8}){
            Text(n===0?'今日':'周'+weekday(n)).fontSize(14).fontColor(MUTED)
            Text(dayAt(n).slice(-2)).fontSize(19).fontWeight(FontWeight.Bold).fontColor(INK)
            Row().width(6).height(6).borderRadius(6).backgroundColor(this.marked.includes(dayAt(n))?TEAL:Color.Transparent)
          }.layoutWeight(1).height(80).justifyContent(FlexAlign.Center).borderRadius(15).backgroundColor(DAY_COLORS[n+6]).shadow({radius:1,color:n===0?'#D1AC55':'#D7DED7',offsetY:3})
            .onClick(()=>this.onNavigate('records/day/'+dayAt(n)))
        },(n:number)=>String(n))
      }
    }.width('100%').height(158).padding(16).backgroundColor(WHITE).borderRadius({topLeft:24,topRight:24})
  }
  @Builder home(){
    this.header()
    Stack({alignContent:Alignment.Top}){
      Image($r('app.media.records_petal')).width('100%').objectFit(ImageFit.Contain).opacity(0.16)
      Scroll(){
      Column(){
        Column({space:5}){
          Text(this.greeting()).fontSize(25).fontWeight(FontWeight.Bold).fontColor(INK)
          Text(this.greetingDetail()).fontSize(18).fontColor(MUTED)
        }.alignItems(HorizontalAlign.Start).width('100%').margin({bottom:18})
        this.cover()
        this.week()
        Column({space:9}){
          Row({space:7}){Image($r('app.media.tm_icon_sliders')).width(18).height(18);Text('我的目标与习惯').fontSize(25).fontWeight(FontWeight.Bold).fontColor(INK).layoutWeight(1)}.width('100%')
          Text('已打卡 '+this.goals.filter((g:Goal)=>g.progress?.today_checked===true).length+'/'+this.goals.length+' · 查看全部').fontSize(16).fontColor('#775795').alignSelf(ItemAlign.End)
          ForEach(this.goals.slice(0,3),(g:Goal)=>{
            Text(g.title+'     '+(g.progress?.today_checked?'已打卡':'打卡 ›')).width('100%').fontSize(17).fontColor(INK)
              .onClick(()=>this.onNavigate('today/goals/'+g.id))
          },(g:Goal)=>String(g.id))
        }.width('100%').padding(16).backgroundColor(WHITE).borderRadius({bottomLeft:24,bottomRight:24}).constraintSize({minHeight:110})
          .id('today-open-goals').onClick(()=>this.onNavigate('today/goals'))
        Row({space:12}){
          Image($r('app.media.tm_today_services')).width(58).height(58).borderRadius(14)
          Column({space:5}){
            Text('服务预约    7类服务').fontSize(21).fontWeight(FontWeight.Bold).fontColor(INK)
            Text('选择上门服务，查看与调整预约意向').fontSize(16).fontColor(MUTED)
          }.alignItems(HorizontalAlign.Start).layoutWeight(1)
        }.padding(16).width('100%').backgroundColor(WHITE).borderRadius(24).margin({top:16})
          .onClick(()=>this.getUIContext().getPromptAction().showToast({message:'敬请期待'}))
        if(this.error){TMNote({label:this.error}).onClick(()=>this.changed())}
      }.width('100%').padding({top:11,left:17,right:17,bottom:22})
      }.height('100%').scrollBar(BarState.Off)
    }.width('100%').layoutWeight(1)
  }
  build(){
    Column(){
      if(this.route.startsWith('today/library/')){TodayLibrary({route:this.route,onNavigate:this.onNavigate})}
      else if(this.route.startsWith('today/reading/')){TodayReading({route:this.route,onNavigate:this.onNavigate})}
      else if(this.route.startsWith('today/goods')){TodayGoods({route:this.route,onNavigate:this.onNavigate})}
      else if(this.route.startsWith('today/goals')||this.route.startsWith('today/reminders')){TodayHabits({route:this.route,onNavigate:this.onNavigate})}
      else{this.home()}
    }.width('100%').height('100%').backgroundColor(PAPER)
  }
}
