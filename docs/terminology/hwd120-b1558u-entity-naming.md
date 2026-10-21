# Entity names — HWD120-B1558U

English and Russian display names for the native Home Assistant device page.
Machine vocabulary follows the [selected model terminology](hwd120-b1558u.md#selected-base-terminology).
Software concepts such as drafts, profiles, execution state and resource counters
are contributor-written labels, not quotations from the appliance manuals.

## Reading the names

`Upper` / `Lower` and `Верхний` / `Нижний` identify the two drums. Controls are
ordered per drum as lifecycle → saved profiles → programme selection → additional
functions → settings. The ordinary and Smart washing selectors remain adjacent. Sensors
place current cycle status before reported settings. Names sort alphabetically
inside each group; this is not a prescribed sequence of appliance actions.
Diagnostic and Configuration retain their existing card assignments.

Names contain zero to four **U+200A HAIR SPACE** characters after the normal
space following the drum prefix. These narrow, invisible spaces keep the groups
contiguous without adding spoken symbols or long category labels. They remain
part of copied names; removing them can change display order. The tables below
include the actual characters. Group headings explain the ordering but are not
additional headings inserted into Home Assistant cards.

The **Programme** control edits local configuration; **Programme now** is
reported by the machine and does not imply a running wash. The same distinction
applies to `программа` / `тек. программа`. An `on` / `вкл`
suffix names a reported boolean condition, not a promise that it is currently
true. False, unknown and unavailable remain distinct states. Reported settings
never come from a draft. Display names are not stable automation identifiers;
user-customized entity names remain user-owned.

## Name register

### Controls — lifecycle

| English | Russian |
| --- | --- |
| Upper     Start | Верхний     пуск |
| Lower     Start | Нижний     пуск |
| Upper     Pause | Верхний     пауза |
| Lower     Pause | Нижний     пауза |
| Upper     Resume | Верхний     возобновить |
| Lower     Resume | Нижний     возобновить |
| Upper     Cancel | Верхний     отмена |
| Lower     Cancel | Нижний     отмена |
| Upper     Standby | Верхний     в ожидание |
| Lower     Standby | Нижний     в ожидание |

### Controls — saved profiles

| English | Russian |
| --- | --- |
| Upper    Save profile | Верхний    сохранить профиль |
| Lower    Save profile | Нижний    сохранить профиль |
| Upper    New profile | Верхний    новый профиль |
| Lower    New profile | Нижний    новый профиль |
| Upper    Saved profile | Верхний    сохранённый профиль |
| Lower    Saved profile | Нижний    сохранённый профиль |

### Controls — programme selection

| English | Russian |
| --- | --- |
| Upper   Programme | Верхний   программа |
| Lower   Programme | Нижний   программа |
| Upper   Smart washing | Верхний   умная стирка |
| Lower   Smart washing | Нижний   умная стирка |

### Controls — additional functions

| English | Russian |
| --- | --- |
| Upper  Water Plus | Верхний  больше воды |
| Lower  Water Plus | Нижний  больше воды |
| Upper  Easy Ironing | Верхний  легкая глажка |
| Lower  Easy Ironing | Нижний  легкая глажка |
| Upper  Intensive | Верхний  интенсивная |
| Lower  Intensive | Нижний  интенсивная |
| Upper  Eco. wash | Верхний  эко-стирка |
| Lower  Eco. wash | Нижний  эко-стирка |
| Upper  Soak wash | Верхний  замачивание |
| Lower  Soak wash | Нижний  замачивание |
| Upper  Night wash | Верхний  ночная стирка |
| Lower  Night wash | Нижний  ночная стирка |

### Controls — settings

| English | Russian |
| --- | --- |
| Upper Wash (min) | Верхний стирка (мин) |
| Lower Wash (min) | Нижний стирка (мин) |
| Upper Temp. (°C) | Верхний темп. (°C) |
| Lower Temp. (°C) | Нижний темп. (°C) |
| Upper Rinses | Верхний полоскание |
| Lower Rinses | Нижний полоскание |
| Upper Spin (RPM) | Верхний скорость (об/мин) |
| Lower Spin (RPM) | Нижний скорость (об/мин) |
| Upper End delay (h) | Верхний задержка времени окончания (ч) |
| Lower End delay (h) | Нижний задержка времени окончания (ч) |
| Upper Stains | Верхний удаление пятен |
| Lower Stains | Нижний удаление пятен |
| Lower Drying | Нижний сушка |

### Controls — appliance power

The appliance-power control is disabled by default; this name does not enable it.

| English | Russian |
| --- | --- |
| Appliance power | Питание машины |

### Sensors — live status

| English | Russian |
| --- | --- |
| Upper   Door lock | Верхний   дверной замок |
| Lower   Door lock | Нижний   дверной замок |
| Upper   Phase | Верхний   этап |
| Lower   Phase | Нижний   этап |
| Upper   State | Верхний   выполнение |
| Lower   State | Нижний   выполнение |
| Upper   Programme now | Верхний   тек. программа |
| Lower   Programme now | Нижний   тек. программа |
| Upper   Time left | Верхний   осталось |
| Lower   Time left | Нижний   осталось |

### Sensors — reported telemetry

| English | Russian |
| --- | --- |
| Upper Water Plus on | Верхний больше воды вкл |
| Lower Water Plus on | Нижний больше воды вкл |
| Upper Easy Ironing on | Верхний легкая глажка вкл |
| Lower Easy Ironing on | Нижний легкая глажка вкл |
| Upper Intensive on | Верхний интенсивная вкл |
| Lower Intensive on | Нижний интенсивная вкл |
| Upper Eco. wash on | Верхний эко-стирка вкл |
| Lower Eco. wash on | Нижний эко-стирка вкл |
| Upper Soak wash on | Верхний замачивание вкл |
| Lower Soak wash on | Нижний замачивание вкл |
| Upper Night wash on | Верхний ночная стирка вкл |
| Lower Night wash on | Нижний ночная стирка вкл |
| Upper Smart wash on | Верхний умная стирка вкл |
| Lower Smart wash on | Нижний умная стирка вкл |
| Upper End delay active | Верхний отср. конца вкл |
| Lower End delay active | Нижний отср. конца вкл |
| Upper Target temp. | Верхний заданная темп. |
| Lower Target temp. | Нижний заданная темп. |
| Upper Wash-stage time | Верхний весь этап стирки |
| Lower Wash-stage time | Нижний весь этап стирки |
| Upper Rinse count | Верхний число полосканий |
| Lower Rinse count | Нижний число полосканий |
| Upper Spin-stage time | Верхний этап отжима |
| Lower Spin-stage time | Нижний этап отжима |
| Lower Drying mode | Нижний режим сушки |

### Diagnostic — machine status and resources

| English | Russian |
| --- | --- |
| Power | Питание |
| Remote control | Дист. управление |
| Local control lock | Местн. блокировка |
| Power-on water | Вода с включения |
| Cumulative water | Всего воды |
| Power-on energy | Энергия с включения |
| Cumulative electricity | Всего электроэнергии |

### Diagnostic — drum counters

| English | Russian |
| --- | --- |
| Upper Cycle count | Верхний число циклов |
| Lower Cycle count | Нижний число циклов |

### Configuration

| English | Russian |
| --- | --- |
| Sound | Звук |
| Child lock | Защита от детей |

## Abbreviation and meaning rules

Full machine terms and their manual sections/pages remain in the
[model glossary](hwd120-b1558u.md#function-value-and-process-bases).
The forms here shorten presentation, not the selected underlying concept.
Programme values, individual stain/fabric choices and process-state labels are
not shortened by these entity-name rules.

| Full base or composed concept, EN / RU | Short form, EN / RU | Reason and retained meaning |
| --- | --- | --- |
| Upper drum; Lower drum / Верхний барабан; Нижний барабан | Upper; Lower / Верхний; Нижний | Omit only the repeated drum noun; retain full direction words in both locales |
| Selected programme / Выбранная программа | Programme / программа | Controls context supplies editable selection; reported Programme now remains separate |
| Programme, reported / программа по данным машины | Programme now / Тек. программа | Distinguish authoritative readback from the draft without claiming a running wash |
| Smart washing fabric / тип ткани для умной стирки | Smart washing / умная стирка | Restore the selected full machine concept; fabric choices remain available in the selector |
| Wash duration / время стирки | Wash (min) / Стирка (мин) | Retain configured duration and the numeric options' unit |
| Washing temperature / Температура | Temp. (°C) / Темп. (°C) | Use the manuals' short temperature label and explicit unit |
| Rinse count, configured / число полосканий в черновике | Rinses / Полоскание | Keep the rinse setting, not the Rinse & Spin programme |
| Spin speed / Скорость | Spin (RPM) / Скорость (об/мин) | Preserve speed through its unit; never substitute stage duration |
| End time delay / Задержка времени окончания | End delay (h) / задержка времени окончания (ч) | Keep full RU completion-delay terminology and hours; English retains its compact end-delay label |
| Stains / Удаление пятен | Unchanged | Retain the full function label |
| Drying / Сушка | Unchanged | Retain the added function, distinct from programme Dry |
| Water Plus; Easy Ironing; Intensive / Больше воды; Легкая глажка; Интенсивная | Unchanged; add on / вкл for reported flags | Keep the selected full functions and approved RU spelling |
| Eco. wash; Soak wash; Night wash / Эко-стирка; Замачивание; Ночная стирка | Unchanged; add on / вкл for reported flags | Keep full labels, including the period in Eco. wash |
| Reported Smart washing mode / режим умной стирки по данным машины | Smart wash on / Умная стирка вкл | Shorten washing to wash, retaining the boolean mode rather than fabric choice |
| Reported active End time delay / активная задержка времени окончания | End delay active / Отср. конца вкл | Keep the active completion-delay flag distinct from its editable hours |
| Start; Pause / Пуск; Пауза | Unchanged | Repeated selected-programme object is supplied by action context |
| Resume operation / возобновления работы | Resume / Возобновить | Use a standalone continuation verb |
| Cancel wash programme / Отмена программы стирки | Cancel / Отмена | Omit repeated programme object; no drainage guarantee |
| Return to Standby mode / возврат в Режим ожидания | Standby / В ожидание | Name the destination, not power-off or a forced unlock |
| Door lock / Дверной замок | Unchanged | Report locking, not physical door position |
| Remaining operation time / Оставшееся время работы | Time left / Осталось | Keep remaining duration, not elapsed or configured time |
| Phase; execution state / этап программы; состояние выполнения | Phase; State / Этап; Выполнение | Keep two separate interpretations; neither is physical motor proof |
| Reported target washing temperature / заданная температура по данным машины | Target temp. / Заданная темп. | Keep target temperature, not a measured water temperature |
| Reported complete wash-stage time / полная продолжительность этапа стирки по данным машины | Wash-stage time / Весь этап стирки | Keep the complete stage duration, not configured main-wash minutes |
| Reported rinse count / число полосканий по данным машины | Rinse count / Число полосканий | Distinguish from the configured Rinses setting |
| Reported spin-stage time / продолжительность этапа отжима по данным машины | Spin-stage time / Этап отжима | Existing sensor duration unit supplies time, never RPM |
| Reported drying mode / режим сушки по данным машины | Drying mode / Режим сушки | Retain mode to distinguish readback from the editable Drying function |
| New profile name; Save profile; Saved profile / имя нового профиля; сохранить профиль; сохранённый профиль | New profile; Save profile; Saved profile / новый профиль; сохранить профиль; сохранённый профиль | Input context supplies name; retain full save/select labels without profile-word abbreviations |
| Remote control / Пульт дистанционного управления | Remote control / Дист. управление | Abbreviate the selected RU setting, not its permission semantics |
| Local control lock / Блокировка локального управления | Local control lock / Местн. блокировка | Keep the local-control flag separate from Child lock |
| Power-on session water/energy / вода/энергия с момента включения | Power-on water/energy / Вода/Энергия с включения | Preserve power-on session scope, not per-programme consumption |
| Cumulative water/electricity / суммарный расход воды/потребление электроэнергии | Cumulative water/electricity / Всего воды/электроэнергии | Preserve cumulative scope, units and statistics |
| Cycle count / число циклов | Unchanged | Keep the quantity and drum distinction |
| Power; Appliance power / питание; питание машины | Unchanged | Keep reported status and appliance control distinct |
| Sound; Child lock / звук; Защита от детей | Sound; Child lock / Звук; Защита от детей | Retain selected full settings |

Russian names continue with a lowercase word after `Верхний` / `Нижний`.
The direction words are not abbreviated. This changes presentation, not the
selected term's spelling or meaning. Preserve `Eco. wash` and the
source spelling behind `Легкая глажка`; abbreviation does not authorize a different
manual/APK alternative. English names are limited to 22 UTF-16 code units,
including drum prefixes and all spaces. Russian fit depends on rendered width,
not an assumed character-to-pixel conversion.

Only individual machine terms are reproduced; Haier retains copyright in its
manuals and application materials. The grouping and software labels are original
contributor wording.

Licensed under [CC BY 4.0](../LICENSE). Attribution: U+ Reborn contributors,
[U+ Reborn repository](https://github.com/Zuz666/uplus-reborn).
