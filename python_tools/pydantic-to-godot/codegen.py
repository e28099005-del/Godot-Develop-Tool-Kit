import typing
import json
import sys
import os
import inspect
import importlib.util
from pathlib import Path
from pydantic import BaseModel
from pydantic.fields import FieldInfo
import time

# --- 配置區 (Configuration) ---

# 源文件夾：你的 Python Schema 存放處 (按 DDD 領域劃分)
SCHEMA_SOURCE_DIR = Path("./schemas")

# 目標文件夾：Godot 腳本輸出的根目錄
GODOT_OUTPUT_DIR = Path("./godot_project/generated")

# --- 統計類 (Statistics) ---
class ConversionStats:
    def __init__(self):
        self.start_time = time.time()
        self.files_scanned = 0
        self.files_success = 0
        self.files_failed = 0
        self.files_skipped = 0 # 沒有模型的空文件
        self.models_found = 0
        self.errors: typing.List[str] = []

    def log_error(self, file: Path, msg: str):
        self.files_failed += 1
        self.errors.append(f"[FAIL] {file.name}: {msg}")

    def print_report(self):
        duration = time.time() - self.start_time
        print("\n" + "="*50)
        print(f" 🏗️  PYDANTIC TO GODOT CONVERSION REPORT")
        print("="*50)
        print(f" ⏱️  Duration      : {duration:.2f}s")
        print(f" 📂 Files Scanned : {self.files_scanned}")
        print(f" ✅ Files Success : {self.files_success}")
        print(f" ⚠️  Files Skipped : {self.files_skipped} (No models found)")
        print(f" ❌ Files Failed  : {self.files_failed}")
        print(f" 📦 Models Found  : {self.models_found}")
        print("-" * 50)
        
        if self.errors:
            print(" 🛑 ERROR DETAILS:")
            for err in self.errors:
                print(f"    {err}")
        else:
            print(" 🎉 All systems operational. No errors detected.")
        print("="*50 + "\n")

# --- 類型映射核心 (Type Mapping Core) ---

TYPE_MAP = {
    int: "int",
    float: "float",
    str: "String",
    bool: "bool",
    dict: "Dictionary",
    list: "Array",
}

def get_gd_type(py_type) -> str:
    """將 Python 類型映射為 Godot 強類型"""
    # 處理 Optional[T] -> Variant
    if typing.get_origin(py_type) is typing.Union and type(None) in typing.get_args(py_type):
        return "Variant"
        
    origin = typing.get_origin(py_type)
    args = typing.get_args(py_type)

    if origin is list:
        inner_type = get_gd_type(args[0])
        return f"Array[{inner_type}]"
    
    if isinstance(py_type, type) and issubclass(py_type, BaseModel):
        return f"{py_type.__name__}Data"

    return TYPE_MAP.get(py_type, "Variant")

def get_default_value_code(field: FieldInfo) -> str:
    """提取 Pydantic 默認值"""
    if field.is_required():
        return ""
    
    val = field.default
    if val is None: return " = null"
    if isinstance(val, bool): return " = true" if val else " = false"
    if isinstance(val, str): return f' = "{val}"'
    if isinstance(val, (int, float)): return f" = {val}"
    if isinstance(val, list): return " = []"
    if isinstance(val, dict): return " = {}"
    return ""

def generate_class_code(model_cls: typing.Type[BaseModel]) -> str:
    """為單個 Pydantic 模型生成 GDScript 類代碼"""
    class_name = f"{model_cls.__name__}Data"
    # 定義表名規則：類名小寫 + s (例如 Weapon -> weapons)
    # 如果未來需要自定義，可以讀取 model_cls.Config
    table_name = model_cls.__name__.lower() + "s"
    fields = model_cls.model_fields
    
    lines = []
    lines.append(f"class_name {class_name}")
    lines.append(f"extends RefCounted") 
    lines.append("")
    
    # [新增] 生成常量 TABLE_NAME，方便上層 Manager 調用或統一管理
    lines.append(f"const TABLE_NAME = \"{table_name}\"")
    lines.append("")

    # 1. 變量聲明
    for name, field in fields.items():
        gd_type = get_gd_type(field.annotation)
        default_val = get_default_value_code(field)
        lines.append(f"var {name}: {gd_type}{default_val}")
    
    lines.append("")

    # 2. from_dict 解析函數 (Deserialize)
    lines.append(f"static func from_dict(data: Dictionary) -> {class_name}:")
    lines.append(f"\tvar instance = {class_name}.new()")
    
    for name, field in fields.items():
        py_type = field.annotation
        origin = typing.get_origin(py_type)
        access_code = f"data['{name}']"
        
        # 邏輯 A: 嵌套列表 List[Model]
        if origin is list and isinstance(typing.get_args(py_type)[0], type) and issubclass(typing.get_args(py_type)[0], BaseModel):
            inner_cls = f"{typing.get_args(py_type)[0].__name__}Data"
            lines.append(f"\tif data.has('{name}'):")
            lines.append(f"\t\tvar raw = {access_code}")
            lines.append(f"\t\tif raw is String: raw = JSON.parse_string(raw)")
            lines.append(f"\t\tif raw is Array:")
            lines.append(f"\t\t\tinstance.{name} = []")
            lines.append(f"\t\t\tfor item in raw:")
            lines.append(f"\t\t\t\tinstance.{name}.append({inner_cls}.from_dict(item))")

        # 邏輯 B: 嵌套單個對象 Model
        elif isinstance(py_type, type) and issubclass(py_type, BaseModel):
             inner_cls = f"{py_type.__name__}Data"
             lines.append(f"\tif data.has('{name}'):")
             lines.append(f"\t\tvar raw = {access_code}")
             lines.append(f"\t\tif raw is String: raw = JSON.parse_string(raw)")
             lines.append(f"\t\tinstance.{name} = {inner_cls}.from_dict(raw)")

        # 邏輯 C: 基礎集合 (List/Dict)
        elif origin in (list, dict):
             lines.append(f"\tif data.has('{name}'):")
             lines.append(f"\t\tvar raw = {access_code}")
             lines.append(f"\t\tif raw is String: instance.{name} = JSON.parse_string(raw)")
             lines.append(f"\t\telse: instance.{name} = raw")

        # 邏輯 D: 基礎類型
        else:
            lines.append(f"\tif data.has('{name}'): instance.{name} = {access_code}")
            
    lines.append("\treturn instance")
    lines.append("")

    # 3. to_dict 序列化函數 (Serialize)
    lines.append(f"func to_dict() -> Dictionary:")
    lines.append(f"\tvar data = {{}}")
    
    for name, field in fields.items():
        py_type = field.annotation
        origin = typing.get_origin(py_type)
        
        # 邏輯 A: 嵌套列表 List[Model]
        if origin is list and isinstance(typing.get_args(py_type)[0], type) and issubclass(typing.get_args(py_type)[0], BaseModel):
             lines.append(f"\tif {name} != null:")
             lines.append(f"\t\tdata['{name}'] = []")
             lines.append(f"\t\tfor item in {name}:")
             lines.append(f"\t\t\tdata['{name}'].append(item.to_dict())")

        # 邏輯 B: 嵌套單個對象 Model
        elif isinstance(py_type, type) and issubclass(py_type, BaseModel):
             lines.append(f"\tif {name} != null:")
             lines.append(f"\t\tdata['{name}'] = {name}.to_dict()")
             
        # 邏輯 C: 基礎類型
        else:
             lines.append(f"\tdata['{name}'] = {name}")
             
    lines.append(f"\treturn data")
    lines.append("")
    
    # 4. SQLite Helper (如果有 id 字段)
    if 'id' in fields:
        # [更新] 使用 TABLE_NAME 常量而不是硬編碼字符串
        lines.append(f"# SQLite Helper")
        lines.append(f"static func get_by_id(db: SQLite, id: String) -> {class_name}:")
        lines.append(f"\tvar result = db.select_rows(TABLE_NAME, \"id = '\" + id + \"'\", [\"*\"])")
        lines.append(f"\tif result.is_empty(): return null")
        lines.append(f"\treturn from_dict(result[0])")

    return "\n".join(lines)

# --- 文件掃描與處理 (File Scanning & Processing) ---

def load_models_from_file(file_path: Path) -> typing.Tuple[typing.List[typing.Type[BaseModel]], str | None]:
    """
    動態加載 Python 文件並提取其中定義的 Pydantic 模型
    返回: (模型列表, 錯誤信息)
    """
    module_name = file_path.stem
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if not spec or not spec.loader:
        return [], "Could not create module spec"
    
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        return [], str(e)
    
    models = []
    for name, obj in inspect.getmembers(module):
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
            if obj.__module__ == module_name: 
                models.append(obj)
    
    return models, None

def process_all_schemas():
    """主流程：遞歸掃描並生成"""
    stats = ConversionStats()
    
    sys.path.insert(0, str(SCHEMA_SOURCE_DIR.resolve()))
    
    if not SCHEMA_SOURCE_DIR.exists():
        print(f"❌ Source directory not found: {SCHEMA_SOURCE_DIR}")
        return

    print(f"🔍 Scanning {SCHEMA_SOURCE_DIR} for schemas...")
    
    for file_path in SCHEMA_SOURCE_DIR.rglob("*.py"):
        if file_path.name == "__init__.py":
            continue
            
        stats.files_scanned += 1
        relative_path = file_path.relative_to(SCHEMA_SOURCE_DIR)
        
        # 提取模型
        models, error = load_models_from_file(file_path)
        
        if error:
            stats.log_error(relative_path, error)
            continue
            
        if not models:
            stats.files_skipped += 1
            continue
            
        stats.models_found += len(models)
        
        # 生成代碼
        try:
            gd_content = ["# GENERATED CODE - DO NOT MODIFY BY HAND", ""]
            for model in models:
                gd_content.append(generate_class_code(model))
                gd_content.append("")
                
            target_path = GODOT_OUTPUT_DIR / relative_path.with_suffix(".gd")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(target_path, "w", encoding="utf-8") as f:
                f.write("\n".join(gd_content))
            
            stats.files_success += 1
            print(f"  ✅ Generated: {target_path}")
            
        except Exception as e:
            stats.log_error(relative_path, f"Generation failed: {str(e)}")

    # 打印最終報告
    stats.print_report()

if __name__ == "__main__":
    process_all_schemas()