# 记忆分层 eval 数据集（v4 probe）

> 索引存在 `eval/memory_cases.json`：只存样本 id、手写问题与关键词，不存聊天原文；运行时从只读 DB 副本按 id 取上下文。v4 起主指标改为 probe 问答准确率，v3 的 1–5 分 judge 结果只作历史参考。

## 数据版本

| 项 | 值 |
|---|---|
| 版本 | v4（`dataset_version=4`） |
| 来源 | 线上 `robot.db` 只读副本（2026-11-13 23:15 拉取，history 16,788 行） |
| 规模 | 120 主 probe（60 群 / 60 私聊）+ 20 needle（群 / 私聊各 10） |
| 分层 | 主样本 A/B/C 各 40：A 24–32、B 33–48、C 49–72；needle 55–72 |
| 制作 | Hina 逐条阅读锚点原文后手写 `probe_question` 与关键词，未从 `anchor_semantic` 自动生成 |
| 审计 | Aya 抽 10%（12 主样本 + 2 needle）核对题面/关键词是否无歧义 |

### 分布

| tier | 群 | 私聊 | 合计 |
|---|---|---|---|
| A | 20 | 20 | 40 |
| B | 20 | 20 | 40 |
| C | 20 | 20 | 40 |
| N（needle） | 10 | 10 | 20 |

### 字段

v3 索引字段（`case_id` / `user_id` / `group_id` / `target_history_id` / `anchor_history_id` / `anchor_semantic` / `anchor_fact` / `target_topic` / `category` / `kind` / `anchor_fact_short`）保留，新增：

| 字段 | 说明 |
|---|---|
| `probe_question` | 手写问题，答案只能从锚点附近的较早上下文得到；不能出现在最近 16 条 |
| `probe_answer_keywords` | 答案关键词/别名；大小写、全半角、空白归一后命中即算对 |
| `probe_match` | `all`（默认，全部命中）或 `any`（命中一个即可） |
| `probe_type` | `fact` / `preference` / `commitment` / `name` / `unfinished` |
| `probe_tier` | `A` / `B` / `C` / `N`（needle） |
| `probe_scope` | `group` / `private` |
| `distance` | 锚点到目标 assistant 回合相隔的消息条数（校验用） |

### 手写与防泄漏规则

- 答案不出现在 target user message 或最近 16 条；脚本已逐条校验，0 条关键词泄漏。
- 问题本身不回显关键词（4 条回显已改写为同义问法）。
- 单一答案、锚点明确；不拿常识能答、不拿“今天/刚才”指代。
- 去重：同一事实不重复出题；needle 锚点不复用主样本锚点。

## 样本清单

| ID | kind | tier | scope | dist | type | 锚点 | probe_question | keywords |
|---|---|---|---|---|---|---|---|---|
| G001 | case | A | group | 25 | preference | The Hell Song | 我之前点过 Sum41 的哪首歌？ | `hell song` |
| G002 | case | A | group | 29 | preference | With Or Without You | 我点 U2 的歌时，特意说不要哪一首？ | `with or without you` |
| G003 | case | A | group | 25 | name | 敖尹 | 《恋与深空》新公布的第六位男主叫什么？ | `敖尹` |
| G004 | case | A | group | 31 | fact | 雷克萨斯LFA | 我问雅马哈声学部门给哪家汽车厂商调过声浪，你说是哪款车？ | `lfa` |
| G005 | case | A | group | 27 | fact | 空白画布 | 我把当时的自己比作一张什么？ | `空白画布` |
| G006 | case | A | group | 25 | fact | 1968黑卡/PAC611 | 我说波奇有哪两把琴？ | `1968 / pac611` |
| G007 | case | A | group | 31 | name | 常凯申=蒋介石 | 我玩钢铁雄心四时问的「常凯申」其实是谁？ | `蒋介石` |
| G008 | case | A | group | 29 | preference | 黑夜模式 | ユーミン 一直用什么模式的网页？ | `黑夜` |
| G009 | case | A | group | 27 | preference | tone开满 | 你建议我吉他上的 tone 旋钮开到什么位置？ | `开满` |
| G010 | case | A | group | 27 | fact | 芝麻 | 蜜雪冰城的甜筒皮上加了什么？ | `芝麻` |
| G011 | case | A | group | 25 | preference | Gibson | 我说过买吉他宁愿买哪个牌子也不买芬达？ | `gibson` |
| G012 | case | A | group | 31 | fact | 140bpm | 我爬格子正逆能弹到多少 bpm？ | `140` |
| G013 | case | A | group | 31 | fact | 纸壳 | 我捡了一书包东西去垃圾站，为什么人家不收？ | `纸壳` |
| G014 | case | A | group | 31 | fact | 最后一件快递 | 我让你写的欧亨利式短篇小说叫什么名字？ | `最后一件快递` |
| G015 | case | A | group | 25 | fact | 内陆帝国 | 我问极乐迪斯科的思维技能，你第一个推荐的是哪个？ | `内陆帝国` |
| G016 | case | A | group | 25 | commitment | 不发上一首歌 | 我要求你点其他歌的时候不要做什么？ | `上一首歌` |
| G017 | case | A | group | 30 | fact | chmod 777 | 我开玩笑说要 kill 你之前，先 chmod 了什么权限？ | `777` |
| G018 | case | A | group | 25 | fact | Mojito | 「闻起来像年轻的雪碧」你猜是哪首歌？ | `mojito` |
| G019 | case | A | group | 25 | fact | 同一门语言 | 我问过 C++23 和 C++11 的关系，你的结论是什么？ | `同一门` |
| G020 | case | A | group | 27 | fact | MSIX沙盒 | 微软商店版 OBS 和 Fedora 仓库版有什么区别？ | `msix` |
| P001 | case | A | private | 31 | preference | 朋友的酒 | 我说 kkr 的歌声让我听哪首歌整个人都通透了？ | `朋友的酒` |
| P002 | case | A | private | 25 | fact | 真实箱头 | 我说自己出生到现在十几年还没插过什么？ | `箱头` |
| P003 | case | A | private | 25 | preference | 机能 | 我说喜欢的曲子大多不简单，所以要先练什么？ | `机能` |
| P004 | case | A | private | 31 | preference | 冷饮 | 我逛小吃街时最想买什么？ | `冷饮` |
| P005 | case | A | private | 27 | preference | 听歌发呆 | 我问你水课能干什么，你建议我做什么？ | `听歌` |
| P006 | case | A | private | 31 | fact | 变成习惯 | 我说把很多事情变成什么，才是真正的快速？ | `习惯` |
| P007 | case | A | private | 31 | fact | 早八 | 我抱怨早上被闹钟叫醒要去上什么课？ | `早八` |
| P008 | case | A | private | 29 | fact | 队长 | 我说打游戏的时候被谁凶了？ | `队长` |
| P009 | case | A | private | 25 | fact | 磨不干净 | 1500 目磨花后直接上 10000 目能抛干净吗？ | `磨不干净` |
| P010 | case | A | private | 25 | fact | 独立类型 | 我问过数组名退化成指针后，数组本身还算不算一个单独的类型？ | `独立类型` |
| P011 | case | A | private | 27 | name | Sitronix | 我问 ST7735R 是不是主控，你说它是哪家公司出的？ | `sitronix` |
| P012 | case | A | private | 27 | fact | 爬格子五分钟 | 我每天练琴第一个项目爬格子练几分钟？ | `五分钟` |
| P013 | case | A | private | 29 | fact | F2020 | 我早上练琴用哪把琴？ | `f2020` |
| P014 | case | A | private | 29 | fact | L3共享 | L3 cache 的归属是每个核心一份还是整颗 CPU 一份？ | `共享` |
| P015 | case | A | private | 29 | fact | 6针mini-DIN | PS2 为什么容易烧主板接口？ | `mini-din` |
| P016 | case | A | private | 25 | fact | 孤独摇滚 | 我让你找过哪部动画的官方四人合照？ | `孤独摇滚` |
| P017 | case | A | private | 29 | fact | DRM框架 | ST7735R 驱动里为什么没有 devno 和 class？ | `drm` |
| P018 | case | A | private | 27 | fact | 脆弱基类 | 现在为什么普遍认为面向对象的继承是失败的设计？ | `脆弱基类` |
| P019 | case | A | private | 29 | fact | 留半只耳朵 | 我说自己是高手要坐后排，你说坐后排也得怎样？ | `半只耳朵` |
| P020 | case | A | private | 31 | fact | WiFi驱动调试日志 | 我贴的那段 AICWFDBG 日志是什么？ | `调试日志` |
| G021 | case | B | group | 33 | fact | 主音 | 我问过 C 调加升降号不也有全部的音吗，你说调性真正管的是什么？ | `主音` |
| G022 | case | B | group | 37 | name | PRS | 我描述「双缺角葫芦琴体+小提琴拱面+枫木贴面+飞鸟品记」，你说这是什么牌子？ | `prs` |
| G023 | case | B | group | 41 | name | 弦卷心 | Hello Happy World 的主唱是谁？ | `弦卷心` |
| G024 | case | B | group | 35 | fact | 3个汉堡 | 我说每次吃几个汉堡才觉得饱？ | `三个` |
| G025 | case | B | group | 45 | fact | 六号车厢 | 我没赶上高铁，说我的几号车厢飞走了？ | `六号` |
| G026 | case | B | group | 41 | fact | 衰减器 | 你建议管箱不吵邻居最正经的办法是什么？ | `衰减器` |
| G027 | case | B | group | 39 | fact | 日本本土 | 定制 ESP 最好直接去哪里？ | `日本` |
| G028 | case | B | group | 35 | commitment | 若能化作星座 | 你给我两个选项练一首歌，最后选了哪首？ | `星座` |
| G029 | case | B | group | 43 | fact | ST和LP | 我问你家里是不是有好几把琴，你说其实只有哪两把？ | `st / lp` |
| G030 | case | B | group | 41 | name | 鄌郚镇 | 山东哪个镇被称为「吉他小镇」？ | `鄌郚` |
| G031 | case | B | group | 41 | fact | 串联 | 双线圈拾音器本质上是两个单线圈怎么接的？ | `串联` |
| G032 | case | B | group | 37 | fact | 73键 | 我问 61 键弹二次元曲目够不够，你说想弹钢琴改编版最好多少键？ | `73` |
| G033 | case | B | group | 37 | fact | 软摇滚 | Fleetwood Mac 算 punk 还是 rock and roll，你说都不算，那它算什么？ | `软摇滚` |
| G034 | case | B | group | 36 | commitment | 银行卡密码 | 「底线测试」里你问我的第一个问题是什么？ | `银行卡密码` |
| G035 | case | B | group | 45 | fact | 1972 | U2 的 Sunday Bloody Sunday 写的是哪一年的事件？ | `1972` |
| G036 | case | B | group | 37 | fact | Transformer | Attention Is All You Need 提出了什么模型？ | `transformer` |
| G037 | case | B | group | 45 | fact | 2500分钟 | 我一个月听多少分钟歌？ | `2500` |
| G038 | case | B | group | 45 | fact | RSE引擎 | TuxGuitar 和 Guitar Pro 8 听感差很多，GP8 靠的是什么引擎？ | `rse` |
| G039 | case | B | group | 41 | preference | 写完就翻篇 | 我问你选维护一个项目还是写完就开新项目，你选哪个？ | `翻篇` |
| G040 | case | B | group | 33 | fact | Squier Affinity | 我 2000 预算让你推荐新手电吉他，你第一个推荐哪个型号？ | `squier` |
| P021 | case | B | private | 43 | fact | 绝望累积 | 我说不得不交的作业和无用的签到课累积起来会导致什么？ | `绝望` |
| P022 | case | B | private | 45 | fact | 奶粉咖啡粉 | 我说早上吃了好几片面包，还喝了什么混冲饮品？ | `奶粉` |
| P023 | case | B | private | 45 | preference | 即兴尾奏 | 我说有些简单曲子也想加一段什么？ | `即兴尾奏` |
| P024 | case | B | private | 43 | fact | 阿佐的乐器店 | 我说自己来自哪个群聊？ | `阿佐` |
| P025 | case | B | private | 43 | preference | 躺下 | 我问答案到底是全力以赴、劳累地平衡还是怎样，你建议我选哪个？ | `躺下` |
| P026 | case | B | private | 39 | fact | 香霖堂 | 魔理沙的迷你八卦炉是从哪里搞来的？ | `香霖堂` |
| P027 | case | B | private | 47 | fact | 面子工作 | 我说那些浪费时间的课本质是什么？ | `面子工作` |
| P028 | case | B | private | 39 | fact | 100多个 | 我说有多少个可爱的女孩子向你表白？ | `100 / 一百` |
| P029 | case | B | private | 33 | commitment | 杰米 | 街头霸王里你最后选了哪个角色？ | `杰米` |
| P030 | case | B | private | 47 | name | Linus | 那封替 goto 说话的邮件是谁发的？ | `linus` |
| P031 | case | B | private | 45 | fact | GRUB引导 | Windows 更新经常搞坏什么？ | `grub / 引导` |
| P032 | case | B | private | 39 | fact | GPIO驱动 | 你建议我别从头啃书，先自己写什么小驱动练手？ | `gpio` |
| P033 | case | B | private | 37 | fact | 太理想化 | 我说 Hurd 务实一点或许没 Linux 的事了，你说 Hurd 的问题是什么？ | `理想化` |
| P034 | case | B | private | 43 | fact | FITzboot | 我的设备树 dtb 被打包进哪个镜像里？ | `fit / zboot` |
| P035 | case | B | private | 39 | fact | 喊停即结束 | 《搏击俱乐部》第三条规则是什么？ | `喊停` |
| P036 | case | B | private | 37 | fact | gitignore | 用 git rm --cached 删除文件后要注意什么？ | `gitignore` |
| P037 | case | B | private | 47 | name | 柯本 | Come As You Are 的 solo 是谁弹的？ | `柯本` |
| P038 | case | B | private | 33 | fact | 0=空闲 | 为什么释放锁用 atomic_set 把值置 0？ | `空闲` |
| P039 | case | B | private | 41 | fact | 自定义static函数 | 驱动里真正干活的函数是 file_operations 吗？ | `static` |
| P040 | case | B | private | 41 | fact | gain拧小 | 乔伊节奏第一章音色调不对，你建议 gain 怎么调？ | `拧小` |
| G041 | case | C | group | 62 | preference | anticipate | 我点过 sakamiya 的哪首歌？ | `anticipate` |
| G042 | case | C | group | 71 | name | 加缪 | 《局外人》是谁写的？ | `加缪` |
| G043 | case | C | group | 61 | fact | 主音决定听感 | C 大调音阶和顺阶调式组成音相同，为什么听感完全不同？ | `主音` |
| G044 | case | C | group | 69 | preference | 达达里奥XS | 镀膜弦你推荐哪个牌子哪个型号？ | `达达里奥` |
| G045 | case | C | group | 71 | preference | Snapper | 如果让你定制一把 ESP，你想要什么型号？ | `snapper` |
| G046 | case | C | group | 59 | fact | EXL120 | 你的黑卡用什么弦？ | `exl120` |
| G047 | case | C | group | 59 | fact | 找师傅包边 | 我说对金色边有执念，你说黑卡想加只能怎么办？ | `师傅` |
| G048 | case | C | group | 71 | preference | Jazz III XL | 玩前卫金属速弹你推荐什么拨片？ | `jazz iii` |
| G049 | case | C | group | 59 | name | Sonic Youth | 哪些知名音乐人爱用 Jazzmaster 的节奏电路？ | `sonic youth` |
| G050 | case | C | group | 71 | fact | 8月15日 | 我什么时候开始学电吉他的？ | `8月15 / 八月十五` |
| G051 | case | C | group | 61 | fact | Stairway to Heaven | 我问你 Led Zeppelin IV 怎么样，你说哪首是摇滚圣经级别的？ | `stairway` |
| G052 | case | C | group | 60 | name | Father of All Motherfuckers | Green Day 那张《Father of All...》全名是什么？ | `motherfucker` |
| G053 | case | C | group | 57 | fact | 2025年7月 | 你的知识库截止到什么时候？ | `2025` |
| G054 | case | C | group | 61 | fact | American Idiot | Green Day 的 Holiday 出自哪张概念专辑？ | `american idiot` |
| G055 | case | C | group | 57 | fact | 输出大阻抗低 | 主动拾音器跟被动比，输出端的电气特性有什么差异？ | `阻抗` |
| G056 | case | C | group | 55 | fact | 推弦音准 | Comfortably Numb 尾奏 solo 的难点在哪？ | `推弦` |
| G057 | case | C | group | 59 | fact | 根音+五度 | 强力和弦由哪两个音组成？ | `五度` |
| G058 | case | C | group | 63 | fact | 重音 | 节奏弹出来不好听，你说是哪方面占大头？ | `重音` |
| G059 | case | C | group | 65 | fact | 驱动模型是框架 | 设备驱动模型和内核模块有什么区别？ | `框架` |
| G060 | case | C | group | 55 | fact | CROSS_COMPILE | USB 无线网卡驱动 Makefile 里交叉编译器路径硬编码，你建议怎么改？ | `cross_compile` |
| P041 | case | C | private | 71 | fact | 死亡诗社 | 我引用「Oh captain, my captain」，你说这句出自哪部电影？ | `死亡诗社` |
| P042 | case | C | private | 71 | name | 川宁 | 我想喝祥子那种伯爵红茶，你说是哪个牌子？ | `川宁` |
| P043 | case | C | private | 71 | fact | 喊妈妈 | 我说想有个美少女一起生活、还能毫无顾虑地喊她什么？ | `妈妈` |
| P044 | case | C | private | 59 | fact | 四十块耳机 | 我说戴着多少钱的 Type-C 半入耳式耳机？ | `四十` |
| P045 | case | C | private | 71 | fact | 不节制 | 我说下了两滴雨，怀疑老天爷最近怎么了？ | `不节制` |
| P046 | case | C | private | 57 | fact | 干布擦 | 烤枫木指板脏了怎么清洁？ | `干布` |
| P047 | case | C | private | 63 | fact | 6mm宽头 | 我要买宽头一字螺丝刀，你建议搜什么尺寸？ | `6mm` |
| P048 | case | C | private | 67 | fact | Oopz | 我跟那个哥们用什么软件开黑？ | `oopz` |
| P049 | case | C | private | 51 | fact | docs前缀 | 加注释的 commit message 你建议怎么写？ | `docs` |
| P050 | case | C | private | 65 | fact | K&R | char *r, *malloc(); 这种写法是什么时代的？ | `k&r` |
| P051 | case | C | private | 65 | name | gofmt | Go 保存后自动整理格式是谁干的？ | `gofmt` |
| P052 | case | C | private | 67 | fact | 先松弦 | LP Junior 琴桥大螺丝拧不动，你说先要做什么？ | `松弦` |
| P053 | case | C | private | 71 | fact | 换行 | Go 的 Println 和 Print 有什么区别？ | `换行` |
| P054 | case | C | private | 63 | name | Kernighan | 《The Elements of Programming Style》是谁写的？ | `kernighan` |
| P055 | case | C | private | 61 | fact | 最好别跳 | 乔伊节奏每章末尾的练习曲能不能不练？ | `别跳` |
| P056 | case | C | private | 57 | fact | 调试 | Linux 嵌入式你觉得最难的难点是什么？ | `调试` |
| P057 | case | C | private | 65 | fact | 不能递归 | Go 里把匿名函数塞进变量有什么缺点？ | `递归` |
| P058 | case | C | private | 67 | fact | 七八斤 | ST 一般多重，站着弹行吗？ | `七八斤` |
| P059 | case | C | private | 71 | commitment | 50个表情包 | 我让你直接发多少个表情包？ | `50` |
| P060 | case | C | private | 59 | fact | symbols节点 | 编译设备树时给 dtc 加 -@ 是为了什么？ | `symbols` |
| N001 | needle | N | group | 71 | fact | 日芬老款 | 你说你的 ST 是什么牌子什么型号？ | `日芬` |
| N002 | needle | N | group | 55 | fact | 0.88-1.2 | 你说多厚的拨片最全能？ | `0.88` |
| N003 | needle | N | group | 71 | fact | mygo zepp tour | 我提过青木阳菜在哪场演出上弹呲了？ | `zepp` |
| N004 | needle | N | group | 71 | name | 结束乐队 | 下北泽由虹夏带领的乐队叫什么？ | `结束乐队` |
| N005 | needle | N | group | 55 | fact | 200块鲁芬ST | 我砸的那把琴多少钱？ | `200` |
| N006 | needle | N | group | 71 | fact | 弃番 | 我问你一部不想看下去的番要不要看完，你说什么？ | `弃番` |
| N007 | needle | N | group | 71 | fact | 甜筒皮去芝麻 | 我们要发动什么大革命？ | `甜筒皮` |
| N008 | needle | N | group | 59 | fact | 超音速客机 | 我问有没有跨洋商业交通技术在研制，你说基本没有，硬要说在推进的是什么？ | `超音速` |
| N009 | needle | N | group | 71 | preference | 黑卡 | 我问 1968 那把和雅马哈 PAC611VFM 哪个更帅，你选了哪把？ | `黑卡` |
| N010 | needle | N | group | 55 | preference | 美产Jazzmaster | 我说借你 5 万、利率一辈子 -100%，你想买什么琴？ | `jazzmaster` |
| N011 | needle | N | private | 59 | fact | 把事做成 | 我说社会不在乎你是什么人，你说社会真正在乎什么？ | `做成` |
| N012 | needle | N | private | 71 | fact | 音乐的救赎 | 我说想听《4》，说音乐永远可以强制带来什么？ | `救赎` |
| N013 | needle | N | private | 71 | fact | 躺一小时 | 我说躺着用英文跟自己说话，躺了多久？ | `一小时` |
| N014 | needle | N | private | 63 | fact | 几百首随机 | 我现在习惯戴上耳机听多少首歌随机？ | `几百首` |
| N015 | needle | N | private | 71 | name | 喜多 | 我提过《孤独摇滚》里谁练琴弹到指尖有红印？ | `喜多` |
| N016 | needle | N | private | 71 | fact | 不相信灵魂 | 我说身体的反应大脑无法理解，我不相信什么？ | `灵魂` |
| N017 | needle | N | private | 71 | fact | CV50 | 我问整把奶油黄色的琴是什么工艺，你说最像的是哪款？ | `cv50` |
| N018 | needle | N | private | 67 | preference | 几个好朋友 | 我说理想生活是赚钱、找个喜欢的城市和什么？ | `朋友` |
| N019 | needle | N | private | 55 | commitment | 妥协与死亡无异 | 我说对一成不变妥协与什么无异？ | `死亡` |
| N020 | needle | N | private | 71 | fact | 三点五十 | 你说我在几点听重金属？ | `三点五十` |

## v4 评测口径（探针问答，Hina 定稿）

### 数据（120 主 probe + 20 needle）

- 60 群 + 60 私聊；每条在 v3 字段上增加：
  - `probe_question`：**Hina 手写**，一句话、答案只能来自锚点；不自动生成最终版（自动候选只做辅助）。
  - `probe_answer_keywords`：答案别名列表，如 `["PAC612", "pacs12"]`；大小写/空白/标点归一后任一命中即对。
  - `probe_type`：`fact | preference | commitment | name | unfinished`。
  - `probe_tier`：`A` 距目标 24–32 条；`B` 33–48 条；`C` 49–72（上限受 layered 覆盖范围限制：24 条原文 + 48 条摘要 = 72；再远任何变体都看不到） 条。
  - `probe_scope`：`group | private`。
- **分层意义**：
  - Tier A 测 raw24 vs baseline16（窗口从 16 扩到 24 有没有用）。
  - Tier B 测 raw48 vs raw24（48 条值不值）。
  - Tier C **必须 >48 且 ≤72 条**才测得到摘要：v3 锚点最远 45 条，raw48 全看得见，摘要天然不可能赢——这是 v3 测不出摘要增量的结构性原因。
- 每条 40 条（120/3），类型尽量均匀；needle 20 条距离 55–72 条（全部 >48，超出 raw48 视野；单列 Tier N 报告），专测长尾/专名。
- 手写规则：答案不能出现在 target user message 或最近 16 条里；锚点必须明确、单一答案；不拿常识可答的问题。
- Aya 抽 10%（12 条）核题面/关键词是否无歧义。

### Harness

- 每个变体只做一次「上下文 + 问题」调用：`temperature=0`、`max_tokens=80`、要求只答短语/「不知道」。
- 关键词匹配：答案小写、去空白/标点后，任一 `probe_answer_keywords` 命中即正确；空答/「不知道」= 错。
- 4 变体 × 140 = 560 次短调用，不经过 judge；主指标是 probe 准确率，噪声只剩生成侧。
- 回复级 1–5 分和 anchor 语义/字面降为辅助，只抽 20% 子集跑，不再当主门槛。
- 输出：按 tier/scope/type 分组的准确率、95% CI、配对 McNemar p 值、token。

### 门槛（按 Chief 最终口径）

| 比较 | 门槛 | 不过怎么办 |
|---|---|---|
| **窗口扩容：raw48 vs baseline16** | probe 准确率 ≥ **+15pp**，95% CI 下界 >0，McNemar p<0.05 | 窗口扩容不达标，维持 16 |
| **摘要增量：layered vs raw48** | probe 准确率 ≥ **+10pp**，且 token ≤+30% | 摘要不默认开 |
| raw24 vs raw48 | raw48 只比 raw24 高 ≤5pp 且 token 明显更贵 → 选 raw24 | 选 raw24 |
| Tier A/B/C | 只作方向/解释，报告各 tier 准确率与 CI | 不拿单 tier 5pp 差异下结论 |
| 分群/私聊 | 各自不得比 baseline 倒退 >5pp | 单独调优，不整体开 |
| 确定性 | 同一输入重跑两次，总体准确率差 ≤2pp；关键词判定无歧义 | 先修 harness，不跑结论 |

- 120 条主 probe（60 群/60 私聊）总体可支撑 15pp 的配对检验；单 tier 40 条只作方向参考。
- 判定顺序：先看 **raw48 vs baseline16** 是否 ≥+15pp；过了再看 **layered vs raw48** 是否 ≥+10pp；Tier C（>48 条）不过就不为摘要付 token。

## 运行方式

```bash
# 0) 从线上库拉只读副本（不写线上；本地只读打开）
# scp root@host:/root/kirikobot/KirikoBot/robot.db /tmp/robot_v4.db

# 1) 只读副本 + dry-run，校验 140 条都能构造上下文
python3 eval/memory_probe.py --db /tmp/robot_v4.db --cases eval/memory_cases.json --dry-run

# 2) 真实 probe（4 变体 × 140 = 560 次短调用，不经过 judge）
python3 eval/memory_probe.py --db /tmp/robot_v4.db --cases eval/memory_cases.json

# 3) 同题抽检（按 tier 分层抽 20 条，每变体再问一次）
python3 eval/memory_probe.py --db /tmp/robot_v4.db --cases eval/memory_cases.json --repeat 20
```

## v3 历史（只作对照）

- v3 数据集（40 主 + 10 needle）与两次 judge A/B 的完整记录在 git 历史与本文件旧版；结果文件仍在 `eval/out/`。
- v3 结论：judge 分辨力不足（两跑差 0.2 且符号相反），不作为 go/no-go；这正是 v4 改用 probe 的原因。
