import {actorId,rpc} from '../services/Cloud';
import {INK,MUTED,PAPER,WHITE,TEAL,TMNote,TMLabel,TMButton,operationId} from './TodayShared';
interface Product{id:number;name:string;price_cents:number;quantity?:number;category:string;description:string;is_active:boolean;}
interface OrderLine{name?:string;product_name?:string;product_id:number;quantity:number;unit_price_cents:number;subtotal_cents:number;}
interface Order{id:number;order_no:string;status:string;total_amount_cents:number;created_at:string;items:OrderLine[];}
interface CloudOrder {
  id:number;order_no?:string;status:string;total_amount_cents:number;created_at:string;
  items?:OrderLine[];product_id:number;quantity:number;unit_price_cents:number;product_name?:string;product_name_snapshot?:string;
}
function orderFromCloud(value:CloudOrder):Order {
  const line:OrderLine={product_id:value.product_id,name:value.product_name||value.product_name_snapshot||'商品',quantity:value.quantity,unit_price_cents:value.unit_price_cents,subtotal_cents:value.total_amount_cents};
  const result:Order={id:value.id,order_no:value.order_no||String(value.id),status:value.status,total_amount_cents:value.total_amount_cents,created_at:value.created_at,items:value.items?.length?value.items:[line]};
  return result;
}
function orderStatus(status:string):string{
  const labels=new Map<string,string>([['created','已提交'],['pending','待处理'],['processing','处理中'],['delivered','已交付'],['completed','已完成'],['cancelled','已取消'],['canceled','已取消'],['failed','未完成']]);
  return labels.get(status)||'状态待确认';
}
function yuan(n:number):string{return Number.isFinite(n)?'¥'+(n/100).toFixed(2):'价格待确认';}
function art(p:Product):Resource{
  if(/燕麦/.test(p.name))return $r('app.media.tm_today_product_oats11854');
  if(/收纳/.test(p.name))return $r('app.media.tm_today_product_storage11854');
  if(/弹力|拉力/.test(p.name))return $r('app.media.tm_today_product_band11854');
  if(/杯|水|壶/.test(p.name))return $r('app.media.tm_today_product_bottle');
  if(/巾/.test(p.name))return $r('app.media.tm_today_product_towel');
  if(/枕/.test(p.name))return $r('app.media.tm_today_product_pillow');
  return $r('app.media.tm_today_goods');
}
const BASE='today/goods';
@Component
export struct TodayGoods{
  @Prop @Watch('changed') route:string=BASE;
  onNavigate:(r:string)=>void=()=>{};
  @State products:Product[]=[];
  @State cart:Product[]=[];
  @State favorites:number[]=[];
  @State orders:Order[]=[];
  @State savedOnly:boolean=false;
  @State category:string='全部';
  @State search:string='';
  @State count:number=1;
  @State busy:boolean=false;
  @State error:string='';
  private serial:number=0;
  private lastKey:string='';
  private lastOp:string='';
  private getEntityId():number{return Number(this.route.split('/')[2])||0;}
  private getItem():Product|undefined{return this.products.find((p:Product)=>p.id===this.getEntityId());}
  private getIsCart():boolean{return this.route===BASE+'/cart';}
  private getIsOrders():boolean{return this.route.startsWith(BASE+'/orders');}
  private getTitle():string{return this.getIsCart()?'购物车':this.route===BASE+'/orders'?'我的订单':this.getIsOrders()?'订单详情':'生活好物';}
  aboutToAppear(){this.changed();}
  aboutToDisappear(){this.serial++;}
  changed():void{this.count=1;this.lastKey='';this.load(++this.serial);}
  private async load(token:number):Promise<void>{this.error='';try{
    const data=await Promise.all([rpc<Product[]>('commerce','list_products',[actorId()],{'include_inactive':false} as Record<string, Object|null>),rpc<Product[]>('commerce','list_cart',[actorId()]),rpc<Product[]>('commerce','list_favorites',[actorId()])]);if(token!==this.serial)return;
    this.products=data[0];this.cart=data[1];this.favorites=data[2].map((p:Product)=>p.id);
    if(this.getIsOrders()){const os=await rpc<CloudOrder[]>('commerce','list_orders',[actorId()]);if(token===this.serial)this.orders=os.map(orderFromCloud);}
  }catch(e){if(token===this.serial)this.error=(e as Error).message;}}
  private async write(method:string,args:Object[],next:string=''):Promise<void>{
    if(this.busy)return;this.busy=true;this.error='';const token=this.serial;
    try{
      const key=method+JSON.stringify(args);if(key!==this.lastKey){this.lastKey=key;this.lastOp=operationId();}
      await rpc<Object>('commerce',method,[actorId(),...args],undefined,this.lastOp);
      if(token!==this.serial)return;this.lastKey='';if(next)this.onNavigate(next);else await this.load(token);
    }catch(e){if(token===this.serial)this.error=(e as Error).message;}finally{this.busy=false;}
  }
  private list():Product[]{return this.products.filter((p:Product)=>(!this.savedOnly||this.favorites.includes(p.id))&&(this.category==='全部'||p.category===this.category)&&(p.name+p.description).includes(this.search.trim()));}
  private categories():string[]{const items:string[]=['全部'];this.products.forEach((p:Product)=>{if(p.category&&!items.includes(p.category))items.push(p.category);});return items;}
  @Builder header(){
    Row({space:9}){
      Text('‹').fontSize(32).width(30).fontColor(TEAL).onClick(()=>this.onNavigate(this.route===BASE?'today':this.getIsOrders()&&this.route!==BASE+'/orders'?BASE+'/orders':BASE))
      Text(this.getTitle()).fontSize(23).fontColor(INK).fontWeight(FontWeight.Bold).layoutWeight(1)
      if(this.getIsCart()){Text('共 '+this.cart.reduce((n:number,p:Product)=>n+(p.quantity||0),0)+' 件').fontSize(16)}
      else{
        SymbolGlyph($r('sys.symbol.star')).fontSize(22).accessibilityText('我的收藏').onClick(()=>{this.savedOnly=!this.savedOnly;if(this.route!==BASE)this.onNavigate(BASE);})
        SymbolGlyph($r('sys.symbol.cart')).fontSize(22).accessibilityText('购物车').onClick(()=>this.onNavigate(BASE+'/cart'))
        Text('订单').fontSize(16).onClick(()=>this.onNavigate(BASE+'/orders'))
      }
    }.height(56).width('100%').padding({left:14,right:18}).backgroundColor(PAPER)
  }
  @Builder tile(p:Product){
    Row({space:12}){
      Image(art(p)).width(92).height(92).borderRadius(13).objectFit(ImageFit.Cover).onClick(()=>this.onNavigate(BASE+'/'+p.id))
      Column({space:6}){
        Text(p.name).fontSize(20).fontWeight(FontWeight.Bold).fontColor(INK).onClick(()=>this.onNavigate(BASE+'/'+p.id))
        Text(yuan(p.price_cents)).fontSize(19).fontColor('#986245')
        if(this.getIsCart()){
          Row({space:13}){Text('−').onClick(()=>this.write('set_cart_quantity',[p.id,Math.max(0,(p.quantity||1)-1)]));Text(String(p.quantity||1));Text('＋').onClick(()=>this.write('set_cart_quantity',[p.id,(p.quantity||1)+1]));Text('移除').fontColor(MUTED).onClick(()=>this.write('set_cart_quantity',[p.id,0]))}.enabled(!this.busy)
        }else{
          Row({space:12}){Text(this.favorites.includes(p.id)?'已收藏':'收藏').fontSize(16).fontColor(TEAL).onClick(()=>this.write('set_favorite',[p.id,!this.favorites.includes(p.id)]));Text('＋').fontColor(TEAL).onClick(()=>this.write('add_to_cart',[p.id,1]))}.enabled(!this.busy)
        }
      }.layoutWeight(1).alignItems(HorizontalAlign.Start)
    }.width('100%').padding(12).backgroundColor(WHITE).borderRadius(18)
  }
  @Builder featured(){
    Swiper(){
      ForEach(this.products.slice(0,4),(p:Product)=>{
        Stack({alignContent:Alignment.BottomStart}){
          Image(art(p)).width('100%').height('100%').objectFit(ImageFit.Cover)
          Column({space:6}){
            Text('本期精选').fontSize(15).fontColor('#604323').backgroundColor('#F6E3BB').borderRadius(8).padding(5)
            Blank().layoutWeight(1)
            Text(p.name).fontSize(23).fontColor('#FFFFFF').fontWeight(FontWeight.Bold)
            Text(yuan(p.price_cents)).fontSize(19).fontColor('#FFFFFF')
          }.width('100%').height('100%').alignItems(HorizontalAlign.Start).padding(15)
            .linearGradient({angle:180,colors:[['#00000000',0],['#AE17251B',1]]})
        }.width('100%').height(180).borderRadius(22).clip(true).onClick(()=>this.onNavigate(BASE+'/'+p.id))
      },(p:Product)=>String(p.id))
    }.height(188).width('100%').autoPlay(this.products.length>1).interval(6000)
  }
  @Builder detail(p:Product){
    Image(art(p)).width('100%').height(220).objectFit(ImageFit.Cover).borderRadius(20)
    TMLabel({label:p.name});TMLabel({label:yuan(p.price_cents)})
    TMLabel({label:'备注说明'});TMNote({label:p.description||p.category})
    Row({space:16}){Text('−').onClick(()=>this.count=Math.max(1,this.count-1));Text(String(this.count));Text('＋').onClick(()=>this.count=Math.min(99,this.count+1));Text('合计 '+yuan(this.count*p.price_cents))}
    TMButton({label:this.favorites.includes(p.id)?'已收藏':'收藏',blocked:this.busy,action:()=>this.write('set_favorite',[p.id,!this.favorites.includes(p.id)])})
    TMButton({label:'加入购物车',primary:true,blocked:this.busy,action:()=>this.write('add_to_cart',[p.id,this.count])})
  }
  @Builder order(o:Order){
    Column({space:9}){TMLabel({label:o.order_no+'　'+orderStatus(o.status)});TMNote({label:o.created_at.slice(0,16).replace('T',' ')})
      ForEach(o.items,(i:OrderLine)=>{Text((i.name||i.product_name||'商品')+'　× '+i.quantity+'　'+yuan(i.subtotal_cents)).fontSize(17).fontColor(INK)})
      TMLabel({label:'合计 '+yuan(o.total_amount_cents)})
    }.padding(14).width('100%').backgroundColor(WHITE).borderRadius(17).onClick(()=>this.onNavigate(BASE+'/orders/'+o.id))
  }
  build(){Column(){
    this.header()
    Scroll(){Column({space:12}){
      if(this.getIsCart()){
        ForEach(this.cart,(p:Product)=>{this.tile(p)},(p:Product)=>String(p.id)+p.quantity)
        if(!this.cart.length){TMNote({label:'购物车还是空的'})}else{
          TMLabel({label:'合计 '+yuan(this.cart.reduce((n:number,p:Product)=>n+p.price_cents*(p.quantity||0),0))})
          TMButton({label:'提交订单',primary:true,blocked:this.busy||this.cart.some((p:Product)=>!p.is_active),action:()=>this.getUIContext().showAlertDialog({title:'提交订单？',message:'请确认商品与数量后提交。',primaryButton:{value:'取消',action:()=>{}},secondaryButton:{value:'提交订单',action:()=>this.write('checkout_cart',[],BASE+'/orders')}})})
        }
      }else if(this.getIsOrders()){
        TMLabel({label:'订单记录'})
        ForEach(this.orders.filter((o:Order)=>this.route===BASE+'/orders'||String(o.id)===this.route.split('/')[3]),(o:Order)=>{this.order(o)},(o:Order)=>String(o.id))
        if(!this.orders.length){TMNote({label:'暂无订单'})}
      }else if(this.getEntityId()){if(this.getItem()){this.detail(this.getItem()!)}}
      else{
        TextInput({placeholder:'搜索商品',text:this.search}).onChange((v:string)=>this.search=v)
        if(!this.savedOnly&&!this.search.trim()&&this.category==='全部'&&this.products.length){this.featured()}
        TMLabel({label:this.savedOnly?'我的收藏':'商品分类'})
        Scroll(){Row({space:8}){ForEach(this.categories(),(c:string)=>{Text(c).fontColor(c===this.category?TEAL:MUTED).padding(7).onClick(()=>this.category=c)})}}.scrollable(ScrollDirection.Horizontal).scrollBar(BarState.Off)
        ForEach(this.categories().filter((c:string)=>c!=='全部'&&this.list().some((p:Product)=>p.category===c)),(c:string)=>{
          TMLabel({label:c+'　'+this.list().filter((p:Product)=>p.category===c).length+' 件'})
          ForEach(this.list().filter((p:Product)=>p.category===c),(p:Product)=>{this.tile(p)},(p:Product)=>String(p.id)+this.favorites.includes(p.id))
        },(c:string)=>c)
        if(!this.list().length){TMNote({label:'暂无符合条件的商品'})}
      }
      if(this.error){TMNote({label:this.error});TMButton({label:'重试',action:()=>this.load(this.serial)})}
    }.width('100%').padding(16).alignItems(HorizontalAlign.Start)}.layoutWeight(1).scrollBar(BarState.Off)
  }.width('100%').height('100%').backgroundColor(PAPER)}
}
