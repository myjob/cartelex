
import unittest
import os
import sys

def suite():
    """
    Creates a test suite that runs all unittests found in the project.
    """
    # Ensure src is in python path
    project_root = os.path.dirname(os.path.abspath(__file__))
    src_path = os.path.join(project_root, 'src')
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Discover tests in 'src' directory
    if os.path.exists(src_path):
        print(f"Discovering tests in {src_path}...")
        suite.addTests(loader.discover(src_path, pattern="test_*.py"))
    
    # Discover tests in 'tests' directory if it exists
    tests_path = os.path.join(project_root, 'tests')
    if os.path.exists(tests_path):
        print(f"Discovering tests in {tests_path}...")
        suite.addTests(loader.discover(tests_path, pattern="test_*.py"))

    return suite

if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite())
    sys.exit(not result.wasSuccessful())
