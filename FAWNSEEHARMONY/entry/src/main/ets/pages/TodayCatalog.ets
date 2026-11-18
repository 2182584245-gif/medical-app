export class CatalogSection {
  
  title:string;
  body:string;
  constructor(title:string, body:string){ this.title=title; this.body=body; }
}
export class CatalogItem {
  
  id:string;
  name:string;
  kind:string;
  category:string;
  summary:string;
  image:Resource;
  sections:CatalogSection[];
  constructor(id:string, name:string, kind:string, category:string, summary:string, image:Resource, sections:CatalogSection[]){ this.id=id; this.name=name; this.kind=kind; this.category=category; this.summary=summary; this.image=image; this.sections=sections; }
}
export const CATALOG:CatalogItem[] = [
  new CatalogItem("broccoli", "西兰花", "foods", "蔬菜", "提供维生素C与膳食纤维",$r('app.media.tm_broccoli'),[
    new CatalogSection("食物状态", "鲜品，生重"),
    new CatalogSection("建议份量", "每餐约100–150克，可与其他蔬菜搭配"),
    new CatalogSection("常见搭配", "豆腐、鸡蛋、糙米饭"),
    new CatalogSection("保存方法", "冷藏保存，食用前充分清洗。"),
    new CatalogSection("营养成分", "每100克可食部；鲜品，生重\n碳水化合物　6.64 g\n能量　34.0 kcal\n脂肪　0.37 g\n钙　47.0 mg\n蛋白质　2.82 g\n膳食纤维　2.6 g\n铁　0.73 mg\n钠　33.0 mg\n维生素 C　89.2 mg"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "不要把它作为唯一蔬菜来源。\n煮熟后的每100克数值与生品不同。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Broccoli, raw\nhttps://fdc.nal.usda.gov/food-details/170379/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("apple", "苹果", "foods", "水果", "带皮鲜果提供膳食纤维",$r('app.media.tm_apple'),[
    new CatalogSection("食物状态", "鲜品，带皮可食部"),
    new CatalogSection("建议份量", "一次约150克，计入全天水果"),
    new CatalogSection("常见搭配", "无糖酸奶、燕麦"),
    new CatalogSection("保存方法", "保持新鲜，食用前洗净；切开后及时食用。"),
    new CatalogSection("营养成分", "每100克可食部；鲜品，带皮可食部\n碳水化合物　13.81 g\n能量　52.0 kcal\n脂肪　0.17 g\n钙　6.0 mg\n膳食纤维　2.4 g\n铁　0.12 mg\n钠　1.0 mg\n蛋白质　0.26 g\n维生素 C　4.6 mg"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "优先完整水果，果汁不能按同等份量替代。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Apples, raw, with skin (Includes foods for USDA's Food Distribution Program)\nhttps://fdc.nal.usda.gov/food-details/171688/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("oats", "燕麦", "foods", "谷薯", "提供碳水化合物、膳食纤维与植物蛋白",$r('app.media.tm_oats'),[
    new CatalogSection("食物状态", "原粒燕麦，干重"),
    new CatalogSection("建议份量", "每次干重30–50克，计入主食总量"),
    new CatalogSection("常见搭配", "牛奶、酸奶、水果"),
    new CatalogSection("保存方法", "干燥密封，按包装说明保存。"),
    new CatalogSection("营养成分", "每100克可食部；原粒燕麦，干重\n膳食纤维　10.6 g\n铁　4.72 mg\n钠　2.0 mg\n蛋白质　16.89 g\n维生素 C　0.0 mg\n脂肪　6.9 g\n碳水化合物　66.27 g\n能量　389.0 kcal\n钙　54.0 mg"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "原粒需煮熟；即食产品按包装说明。\n加糖燕麦脆与纯燕麦营养差异较大。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Oats (Includes foods for USDA's Food Distribution Program)\nhttps://fdc.nal.usda.gov/food-details/169705/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("brown-rice", "糙米", "foods", "谷薯", "提供碳水化合物与膳食纤维",$r('app.media.tm_brown_rice'),[
    new CatalogSection("食物状态", "长粒糙米，干重"),
    new CatalogSection("建议份量", "每餐干重约40–75克，与其他主食合计"),
    new CatalogSection("常见搭配", "鱼肉、豆腐、蔬菜"),
    new CatalogSection("保存方法", "干燥密封，煮熟后及时食用或冷藏。"),
    new CatalogSection("营养成分", "每100克可食部；长粒糙米，干重\n钙　9.0 mg\n脂肪　3.2 g\n碳水化合物　76.25 g\n能量　367.0 kcal\n膳食纤维　3.6 g\n铁　1.29 mg\n钠　5.0 mg\n蛋白质　7.54 g\n维生素 C　0.0 mg"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "生米克数不能直接当成米饭克数。\n牙口不好时充分浸泡、煮软。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Rice, brown, long-grain, raw (Includes foods for USDA's Food Distribution Program)\nhttps://fdc.nal.usda.gov/food-details/169703/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("milk", "纯牛奶", "foods", "奶豆", "提供蛋白质与钙",$r('app.media.tm_milk'),[
    new CatalogSection("食物状态", "全脂液态奶，未额外强化维生素A和D"),
    new CatalogSection("建议份量", "可每次约250克，全天奶类约300–500克"),
    new CatalogSection("常见搭配", "燕麦、鸡蛋、全谷物"),
    new CatalogSection("保存方法", "按包装冷藏或常温保存要求，开封后及时饮用。"),
    new CatalogSection("营养成分", "每100克可食部；全脂液态奶，未额外强化维生素A和D\n碳水化合物　4.78 g\n能量　61.0 kcal\n钙　113.0 mg\n脂肪　3.27 g\n维生素 C　0.0 mg\n膳食纤维　0.0 g\n铁　0.03 mg\n钠　43.0 mg\n蛋白质　3.15 g"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "乳糖不耐受者可选无乳糖奶或适合自己的奶制品。\n牛奶过敏者不适用。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Milk, whole, 3.25% milkfat, without added vitamin A and vitamin D\nhttps://fdc.nal.usda.gov/food-details/172217/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("tofu", "豆腐", "foods", "奶豆", "提供植物蛋白与钙",$r('app.media.tm_tofu'),[
    new CatalogSection("食物状态", "硫酸钙凝固的结实豆腐，未烹调"),
    new CatalogSection("建议份量", "每餐约75–100克，可替换部分肉类"),
    new CatalogSection("常见搭配", "西兰花、杂粮饭"),
    new CatalogSection("保存方法", "冷藏保存，充分加热，及时食用。"),
    new CatalogSection("营养成分", "每100克可食部；硫酸钙凝固的结实豆腐，未烹调\n铁　2.66 mg\n膳食纤维　2.3 g\n碳水化合物　2.78 g\n能量　144.0 kcal\n蛋白质　17.27 g\n维生素 C　0.2 mg\n钠　14.0 mg\n钙　683.0 mg\n脂肪　8.72 g"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "此条为硫酸钙凝固豆腐；其他凝固剂和含水量的豆腐不能直接套用钙与蛋白质数值。\n大豆过敏者不适用。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Tofu, raw, firm, prepared with calcium sulfate\nhttps://fdc.nal.usda.gov/food-details/172475/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("egg", "鸡蛋", "foods", "肉蛋鱼", "提供优质蛋白质",$r('app.media.tm_egg'),[
    new CatalogSection("食物状态", "鲜鸡蛋，去壳生重"),
    new CatalogSection("建议份量", "一个中等鸡蛋可食部约50克"),
    new CatalogSection("常见搭配", "蔬菜、全谷物、牛奶"),
    new CatalogSection("保存方法", "冷藏保存，避免破损，烹调至蛋白蛋黄凝固。"),
    new CatalogSection("营养成分", "每100克可食部；鲜鸡蛋，去壳生重\n能量　143.0 kcal\n蛋白质　12.56 g\n铁　1.75 mg\n钠　142.0 mg\n钙　56.0 mg\n脂肪　9.51 g\n碳水化合物　0.72 g\n维生素 C　0.0 mg\n膳食纤维　0.0 g"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "鸡蛋不能替代全部鱼、肉、豆、奶。\n蛋类过敏者不适用。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Egg, whole, raw, fresh\nhttps://fdc.nal.usda.gov/food-details/171287/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("salmon", "三文鱼", "foods", "肉蛋鱼", "提供蛋白质与脂肪",$r('app.media.tm_salmon'),[
    new CatalogSection("食物状态", "养殖大西洋鲑，生重可食部"),
    new CatalogSection("建议份量", "一次约75–100克，搭配蔬菜主食"),
    new CatalogSection("常见搭配", "杂粮饭、深色蔬菜"),
    new CatalogSection("保存方法", "冷藏并尽快烹调；不能及时食用时按食品安全要求冷冻。"),
    new CatalogSection("营养成分", "每100克可食部；养殖大西洋鲑，生重可食部\n能量　208.0 kcal\n钙　9.0 mg\n脂肪　13.42 g\n蛋白质　20.42 g\n铁　0.34 mg\n钠　59.0 mg\n膳食纤维　0.0 g\n维生素 C　3.9 mg\n碳水化合物　0.0 g"),
    new CatalogSection("说明", "通用食材参考值，品种、产地、加工与含水量会造成差异；包装食品以标签为准。"),
    new CatalogSection("温馨提示", "本条采用养殖大西洋鲑通用数据，野生品种不同。\n建议充分熟制，鱼类过敏者不适用。"),
    new CatalogSection("资料来源", "美国农业部食物成分数据库：Fish, salmon, Atlantic, farmed, raw\nhttps://fdc.nal.usda.gov/food-details/175167/nutrients\n国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml"),
  ]),
  new CatalogItem("tomato-beef", "番茄牛肉炖菜", "recipes", "家常热菜", "瘦牛肉提供蛋白质，搭配番茄与洋葱",$r('app.media.tm_tomato_beef'),[
    new CatalogSection("份量与时间", "2 人份　80 分钟　家常"),
    new CatalogSection("所需食材", "瘦牛肩肉　200克\n番茄　300克\n洋葱　100克\n橄榄油　6克\n食盐　1克"),
    new CatalogSection("制作步骤", "1. 备料　洗净蔬菜、切块；牛肉与蔬菜使用分开的刀板。\n2. 炒香　锅中加入所列油量，洋葱和番茄轻炒出汁。\n3. 炖熟　加入牛肉和适量热水，煮开后转小火炖至牛肉软熟。\n4. 调味分餐　加入所列食盐，再加热后分成两份；搭配一份主食。"),
    new CatalogSection("营养成分", "每份（全配方的1/2）\n能量　205.5 kcal\n碳水化合物　10.5 g\n蛋白质　24.0 g\n脂肪　7.5 g\n膳食纤维　2.6 g\n钠　277.3 mg"),
    new CatalogSection("说明", "参考膳食指南自拟搭配；营养由所列可食部生重和USDA通用数据相加估算，包含全部列出的油和盐，不是化验结果；未使用烹调保留率。"),
    new CatalogSection("温馨提示", "接触生肉后洗手，避免生熟交叉污染。\n仅凭时间不能确定熟度，炖至中心充分加热、肉质软熟。\n本菜不含额外酱料；加酱料后应重新考虑钠和能量。"),
    new CatalogSection("资料来源", "国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml\n世界卫生组织：健康膳食（2026）\nhttps://www.who.int/news-room/fact-sheets/detail/healthy-diet\n美国食品药品监督管理局：安全最低烹饪温度\nhttps://www.fda.gov/food/buy-store-serve-safe-food/safe-food-handling\n美国农业部食物成分数据库：Beef, chuck, arm pot roast, separable lean only, trimmed to 1/8\" fat, all grades, raw\nhttps://fdc.nal.usda.gov/food-details/174051/nutrients\n美国农业部食物成分数据库：Tomatoes, red, ripe, raw, year round average\nhttps://fdc.nal.usda.gov/food-details/170457/nutrients\n美国农业部食物成分数据库：Onions, raw\nhttps://fdc.nal.usda.gov/food-details/170000/nutrients\n美国农业部食物成分数据库：Oil, olive, salad or cooking\nhttps://fdc.nal.usda.gov/food-details/171413/nutrients\n美国农业部食物成分数据库：Salt, table\nhttps://fdc.nal.usda.gov/food-details/173468/nutrients"),
  ]),
  new CatalogItem("broccoli-tofu", "西兰花豆腐", "recipes", "蔬菜豆品", "豆腐提供植物蛋白，西兰花提供膳食纤维",$r('app.media.tm_broccoli_tofu'),[
    new CatalogSection("份量与时间", "1 人份　20 分钟　家常"),
    new CatalogSection("所需食材", "西兰花　200克\n豆腐　100克\n橄榄油　5克\n食盐　0.5克"),
    new CatalogSection("制作步骤", "1. 清洗切块　西兰花分小朵，豆腐切块；处理干净。\n2. 充分加热　先将西兰花焯水，沥干后与豆腐一起入锅。\n3. 轻炒调味　加入所列油量，轻炒至熟，最后加入所列盐量。\n4. 搭配主食　盛盘，配杂粮饭等主食；不把本菜当成完整一日饮食。"),
    new CatalogSection("营养成分", "每份（全配方的1/1）\n能量　256.2 kcal\n碳水化合物　16.1 g\n蛋白质　22.9 g\n脂肪　14.5 g\n膳食纤维　7.5 g\n钠　273.9 mg"),
    new CatalogSection("说明", "参考膳食指南自拟搭配；营养由所列可食部生重和USDA通用数据相加估算，包含全部列出的油和盐，不是化验结果；未使用烹调保留率。"),
    new CatalogSection("温馨提示", "大豆过敏者应换成自己适用的蛋白质食物。\n牙口不好时把菜煮软、豆腐切小块。\n熟食做好后及时吃，剩余部分妥善冷藏并充分复热。"),
    new CatalogSection("资料来源", "国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml\n世界卫生组织：健康膳食（2026）\nhttps://www.who.int/news-room/fact-sheets/detail/healthy-diet\n美国食品药品监督管理局：安全最低烹饪温度\nhttps://www.fda.gov/food/buy-store-serve-safe-food/safe-food-handling\n美国农业部食物成分数据库：Broccoli, raw\nhttps://fdc.nal.usda.gov/food-details/170379/nutrients\n美国农业部食物成分数据库：Tofu, raw, firm, prepared with calcium sulfate\nhttps://fdc.nal.usda.gov/food-details/172475/nutrients\n美国农业部食物成分数据库：Oil, olive, salad or cooking\nhttps://fdc.nal.usda.gov/food-details/171413/nutrients\n美国农业部食物成分数据库：Salt, table\nhttps://fdc.nal.usda.gov/food-details/173468/nutrients"),
  ]),
  new CatalogItem("oat-yogurt", "燕麦酸奶碗", "recipes", "早餐加餐", "燕麦补充碳水，原味酸奶提供蛋白质和钙",$r('app.media.tm_oat_yogurt'),[
    new CatalogSection("份量与时间", "1 人份　15 分钟　家常"),
    new CatalogSection("所需食材", "原粒燕麦　40克\n原味全脂酸奶　150克\n苹果可食部　100克"),
    new CatalogSection("制作步骤", "1. 煮熟燕麦　原粒燕麦按包装说明加水煮熟，放至温热。\n2. 清洗水果　苹果洗净，去核切成适合入口的小块。\n3. 组合装碗　加入原味酸奶和苹果块，拌匀即可。\n4. 按需搭配　根据本餐需要搭配鸡蛋等食物，不额外加糖浆。"),
    new CatalogSection("营养成分", "每份（全配方的1/1）\n能量　299.1 kcal\n碳水化合物　47.3 g\n蛋白质　12.2 g\n脂肪　7.8 g\n膳食纤维　6.6 g\n钠　70.8 mg"),
    new CatalogSection("说明", "参考膳食指南自拟搭配；营养由所列可食部生重和USDA通用数据相加估算，包含全部列出的油和盐，不是化验结果；未使用烹调保留率。"),
    new CatalogSection("温馨提示", "选择原味酸奶；乳饮料或加糖酸奶不能直接套用此营养值。\n牛奶过敏者需换用适合自己的配料，营养也会改变。\n酸奶按标签要求冷藏，组合后及时食用。"),
    new CatalogSection("资料来源", "国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml\n世界卫生组织：健康膳食（2026）\nhttps://www.who.int/news-room/fact-sheets/detail/healthy-diet\n美国食品药品监督管理局：安全最低烹饪温度\nhttps://www.fda.gov/food/buy-store-serve-safe-food/safe-food-handling\n美国农业部食物成分数据库：Oats (Includes foods for USDA's Food Distribution Program)\nhttps://fdc.nal.usda.gov/food-details/169705/nutrients\n美国农业部食物成分数据库：Yogurt, plain, whole milk\nhttps://fdc.nal.usda.gov/food-details/171284/nutrients\n美国农业部食物成分数据库：Apples, raw, with skin (Includes foods for USDA's Food Distribution Program)\nhttps://fdc.nal.usda.gov/food-details/171688/nutrients"),
  ]),
  new CatalogItem("steamed-fish", "清蒸鱼配杂粮饭", "recipes", "均衡主餐", "鱼肉提供蛋白质，糙米与蔬菜搭配",$r('app.media.tm_steamed_fish'),[
    new CatalogSection("份量与时间", "1 人份　40 分钟　家常"),
    new CatalogSection("所需食材", "鳕鱼可食部　120克\n糙米干重　60克\n西兰花　150克\n橄榄油　5克\n食盐　0.5克"),
    new CatalogSection("制作步骤", "1. 处理食材　糙米洗净；鳕鱼查除鱼刺，西兰花清洗分朵。\n2. 先煮主食　糙米按包装说明浸泡和煮饭，煮至软熟。\n3. 蒸熟鱼蔬菜　鳕鱼蒸熟；蔬菜另行加热，中心熟透后加入所列油盐。\n4. 分盘食用　鱼、饭、蔬菜同盘；吃鱼前再次注意鱼刺。"),
    new CatalogSection("营养成分", "每份（全配方的1/1）\n能量　413.8 kcal\n碳水化合物　55.7 g\n蛋白质　30.1 g\n脂肪　8.3 g\n膳食纤维　6.1 g\n钠　311.2 mg"),
    new CatalogSection("说明", "参考膳食指南自拟搭配；营养由所列可食部生重和USDA通用数据相加估算，包含全部列出的油和盐，不是化验结果；未使用烹调保留率。"),
    new CatalogSection("温馨提示", "食品安全参考：鱼中心达到63℃，或肉质不透明且易用叉分开。\n此营养是生配料估算，不表示可生吃鱼。\n额外酱油、沙拉酱和糖不在该配方内。"),
    new CatalogSection("资料来源", "国家卫生健康委：健康饮食、合理膳食核心信息（2025）\nhttps://www.nhc.gov.cn/sps/c100088/202505/6b7a718abd4848e8a3e98fc561a8858b.shtml\n世界卫生组织：健康膳食（2026）\nhttps://www.who.int/news-room/fact-sheets/detail/healthy-diet\n美国食品药品监督管理局：安全最低烹饪温度\nhttps://www.fda.gov/food/buy-store-serve-safe-food/safe-food-handling\n美国农业部食物成分数据库：Fish, cod, Atlantic, raw\nhttps://fdc.nal.usda.gov/food-details/171955/nutrients\n美国农业部食物成分数据库：Rice, brown, long-grain, raw (Includes foods for USDA's Food Distribution Program)\nhttps://fdc.nal.usda.gov/food-details/169703/nutrients\n美国农业部食物成分数据库：Broccoli, raw\nhttps://fdc.nal.usda.gov/food-details/170379/nutrients\n美国农业部食物成分数据库：Oil, olive, salad or cooking\nhttps://fdc.nal.usda.gov/food-details/171413/nutrients\n美国农业部食物成分数据库：Salt, table\nhttps://fdc.nal.usda.gov/food-details/173468/nutrients"),
  ]),
];
