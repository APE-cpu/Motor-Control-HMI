"""「查看模型结构」文本报告：按层汇总参数量（无 GUI 依赖）。"""


def describe_model(name: str, hp: dict) -> list:
    lines = [f"模型：{name}", ""]
    if name.startswith(("随机森林", "支持向量机")):
        lines.append("（scikit-learn 模型，无神经网络层结构）")
        for k, v in hp.items():
            lines.append(f"  {k} = {v}")
        return lines
    try:
        from training.trainer import _build_torch_model
        model = _build_torch_model(name, hp)
        total = 0
        # 按层分组：把同一层的 weight/bias 合并为一行
        layer_params: dict = {}
        for pname, p in model.named_parameters():
            parts = pname.rsplit(".", 1)
            layer = parts[0] if len(parts) == 2 else pname
            kind = parts[1] if len(parts) == 2 else "param"
            layer_params.setdefault(layer, {})[kind] = p
            total += p.numel()
        # 找出对应的 nn.Module 类型
        named_mods = {n: m for n, m in model.named_modules() if n}
        for layer, params in layer_params.items():
            mod = named_mods.get(layer)
            mod_type = type(mod).__name__ if mod else "Layer"
            w = params.get("weight")
            b = params.get("bias")
            if w is not None:
                in_f = w.shape[1] if w.dim() > 1 else w.shape[0]
                out_f = w.shape[0]
                w_cnt = w.numel()
                b_cnt = b.numel() if b is not None else 0
                shape_str = f"[{in_f} → {out_f}]"
                param_str = f"{w_cnt:,} + {b_cnt:,} = {w_cnt+b_cnt:,} 参数" if b is not None else f"{w_cnt:,} 参数"
                lines.append(f"  {mod_type:15s}  {shape_str:15s}  {param_str}")
            else:
                for k, p in params.items():
                    lines.append(f"  {layer}.{k:30s}  {str(list(p.shape)):15s}  {p.numel():,} 参数")
        lines += ["", f"总参数量：{total:,}"]
    except Exception as e:
        lines.append(f"（无法加载模型：{e}）")
    return lines
