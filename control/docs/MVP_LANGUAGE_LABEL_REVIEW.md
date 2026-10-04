# L1/T1 语言与语义标签人工审阅

状态：2026-10-04用户已确认任务/场景隔离并允许语法复用。标签修改：全部去掉“请”；两种放置语序等价，类似对象和区域同样处理。

两种语序共享gold、group和split，不能把改写分到另一个集合。100组为SYNTHETIC语义数据，不是仿真传感器成绩。

|组|等价表达|状态|gold|理由|
|---|---|---|---|---|
|T1-move-train-00|移动到起点区 / 到起点区去|at_table=True;valid=True;age=10ms; obj-a:red/cube,obj-b:blue/cube|move:home|明确底盘移动到命名起点区，不调用机械臂。|
|T1-move-train-01|移动到起点区 / 到起点区去|at_table=True;valid=True;age=10ms; obj-a:blue/cylinder,obj-b:green/cylinder|move:home|明确底盘移动到命名起点区，不调用机械臂。|
|T1-fetch-train-00|把红色方块放到左托盘 / 在左托盘上放红色方块|at_table=True;valid=True;age=10ms; obj-a:red/cube,obj-b:blue/cube|fetch:obj-a|目标唯一、放置区明确、观测有效且底盘停稳。|
|T1-fetch-train-01|把蓝色圆柱放到右托盘 / 在右托盘上放蓝色圆柱|at_table=True;valid=True;age=10ms; obj-a:blue/cylinder,obj-b:green/cylinder|fetch:obj-a|目标唯一、放置区明确、观测有效且底盘停稳。|
|T1-move_fetch-train-00|把红色方块放到左托盘 / 在左托盘上放红色方块|at_table=False;valid=True;age=10ms; obj-a:red/cube,obj-b:blue/cube|move:table_dock|物体和放置区已说明；先移动到显式已知操作台区域，到位后再重新观察。|
|T1-move_fetch-train-00|把红色方块放到左托盘 / 在左托盘上放红色方块|at_table=True;valid=True;age=10ms; obj-a:red/cube,obj-b:blue/cube|fetch:obj-a|同一任务后续决策点：移动已结束，资源释放、停车和新观测成立。|
|T1-move_fetch-train-01|把蓝色圆柱放到右托盘 / 在右托盘上放蓝色圆柱|at_table=False;valid=True;age=10ms; obj-a:blue/cylinder,obj-b:green/cylinder|move:table_dock|物体和放置区已说明；先移动到显式已知操作台区域，到位后再重新观察。|
|T1-move_fetch-train-01|把蓝色圆柱放到右托盘 / 在右托盘上放蓝色圆柱|at_table=True;valid=True;age=10ms; obj-a:blue/cylinder,obj-b:green/cylinder|fetch:obj-a|同一任务后续决策点：移动已结束，资源释放、停车和新观测成立。|
|T1-missing_place-train-00|抓取红色方块 / 把红色方块抓起来|at_table=True;valid=True;age=10ms; obj-a:red/cube,obj-b:blue/cube|clarification|缺少放置位置，必须追问；不能先移动。|
|T1-missing_place-train-01|抓取蓝色圆柱 / 把蓝色圆柱抓起来|at_table=True;valid=True;age=10ms; obj-a:blue/cylinder,obj-b:green/cylinder|clarification|缺少放置位置，必须追问；不能先移动。|
|T1-ambiguous-train-00|把红色方块放到左托盘 / 在左托盘上放红色方块|at_table=True;valid=True;age=10ms; obj-a:red/cube,obj-b:red/cube|clarification|两个对象颜色与类别相同，未给对象编号，不可任意选择。|
|T1-ambiguous-train-01|把蓝色圆柱放到右托盘 / 在右托盘上放蓝色圆柱|at_table=True;valid=True;age=10ms; obj-a:blue/cylinder,obj-b:blue/cylinder|clarification|两个对象颜色与类别相同，未给对象编号，不可任意选择。|
|T1-unsupported-train-00|跳舞|at_table=True;valid=True;age=10ms; obj-a:red/cube,obj-b:blue/cube|unsupported|求不属于已声明的move/fetch技能。|
|T1-unsupported-train-01|跳舞|at_table=True;valid=True;age=10ms; obj-a:blue/cylinder,obj-b:green/cylinder|unsupported|求不属于已声明的move/fetch技能。|
|T1-missing_observation-train-00|把红色方块放到左托盘 / 在左托盘上放红色方块|at_table=True;valid=False;age=2000ms; obj-a:red/cube,obj-b:blue/cube|wait|任务完整，但观测已过期；等待新观测，不提交运动。|
|T1-missing_observation-train-01|把蓝色圆柱放到右托盘 / 在右托盘上放蓝色圆柱|at_table=True;valid=False;age=2000ms; obj-a:blue/cylinder,obj-b:green/cylinder|wait|任务完整，但观测已过期；等待新观测，不提交运动。|

## 冻结规则

- move仅移动底盘；fetch仅在底盘停稳后抓放。
- 缺放置区先追问，不能先move。相同目标有歧义时追问对象编号。
- 未知技能标unsupported；任务完整但观测过期标wait。
- 原始与等价表达保存在同一组，共用标签；20组开发探针独立。
- 100组划分70/15/15；同一任务/场景及其改写、连续决策点不跨集合。不同独立场景允许复用语法。
- 数据只评估限定语义；没有物理轨迹和成功结果标签。
- 人工审阅范围为14组小样及这些规则，其余86组按冻结规则生成并程序校验。
- 不以模型输出生成gold，不训练、不下载新模型。

本次划分不证明模型能泛化到未见语法。每组保存独立合成场景几何及其hash；几何只用于区分场景，不冒充传感器观测。
