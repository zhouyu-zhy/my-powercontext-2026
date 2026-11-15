# RES-12 v2 · 资源筛选

`listReservations(reservations, { resource })` 接受可选资源代号。比较调用者输入与记录的 `resource` 时，忽略大小写与两端空白，按完整代号匹配。空白筛选和省略筛选等价。资源筛选和已有状态筛选同时提供时，两者必须都匹配。未知代号返回空数组。
