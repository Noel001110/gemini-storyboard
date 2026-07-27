import sys
import os
sys.path.append("/Users/noel/gemini-storyboard")
from engine.prompts import generate_titles
titles = generate_titles("This is a test script about finance.", n=5)
print(titles)
