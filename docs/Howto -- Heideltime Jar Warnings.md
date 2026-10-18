These warnings are annoying but generally harmless. They are happening for two main reasons:

1. **Sloppy Library Code:** The authors of `py_heideltime` did not properly close file handles when reading and writing configuration files. Python's garbage collector catches this and throws a `ResourceWarning`.
    
2. **Python 3.12 Strictness & Debuggers:** Python 3.12 introduced a `DeprecationWarning` when `os.fork()` is used in a multi-threaded process. Because you are running your code through a VS Code debugger (`debugpy`), which is multi-threaded, and `py_heideltime` spawns a Java subprocess, Python 3.12 complains.
    

Here are the two ways to handle this, depending on whether you want to hide the noise or fix the underlying library.

---

### Option 1: The Pragmatic Fix (Suppress the Warnings)

Since these warnings come from a third-party library and don't actually break your code, the standard Pythonic approach is to simply suppress them at the top of your script.

Add this to the very beginning of your Python file:

Python

```
import warnings

# Suppress unclosed file and running subprocess warnings from py_heideltime
warnings.filterwarnings("ignore", category=ResourceWarning)

# Suppress the Python 3.12 os.fork() warning triggered by the debugger/subprocess
warnings.filterwarnings("ignore", category=DeprecationWarning, module="subprocess")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydev_monkey")

import py_heideltime  # Import the library AFTER setting the filters
```

### Option 2: The Root Cause Fix (Patch the Library)

If you hate having warnings in your console and want to fix the library itself, you can edit the `py_heideltime` source code on your local machine.

1. Open the file mentioned in your traceback:
    
    `~/a/celex-facts/.venv/lib/python3.12/site-packages/py_heideltime/config.py`
    
2. Go to lines **16 through 19**. It probably looks something like this:
    
    Python
    
    ```
    # Original bad code
    conf_template = (LIBRARY_PATH / "resources" / "config_props_template").open().read()
    conf_content = conf_template.replace("{lang}", language)
    Path("config.props").open("w").write(conf_content)
    ```
    
3. Replace those lines with this modern `pathlib` approach, which automatically handles opening and closing the files properly:
    
    Python
    
    ```
    # New fixed code
    template_path = LIBRARY_PATH / "resources" / "config_props_template"
    conf_template = template_path.read_text(encoding="utf-8")
    
    conf_content = conf_template.replace("{lang}", language)
    
    Path("config.props").write_text(conf_content, encoding="utf-8")
    ```
    

### What about the `subprocess is still running` warning?

This happens because `py_heideltime` boots up a background Java process (the JAR file) to do the actual text analysis, and the Python script finishes or destroys the Python object before the Java process fully terminates.

If you are using `py_heideltime` inside a loop or a function, you might be creating multiple instances of it. Try to instantiate `py_heideltime.HeidelTime()` only **once** globally and reuse that instance across your script, rather than creating a new instance for every piece of text you want to process.