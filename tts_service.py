import os
from TTS.api import TTS


tts = None

def get_engine():
    global tts
    if tts is None:
        tts = TTS(model_name="tts_models/en/ljspeech/tacotron2-DDC", progress_bar=False, gpu=False)
    
    return tts

def synthesize_conqui(text: str, filename: str) -> str:
    if not filename:
        filename = os.path.join(r"C:\Users\GGPC\Desktop\Work\Personal Projects\TTS_Notion", "notion_output.mp3")

        
    tts = get_engine()
    tts.tts_to_file(text=text, file_path=filename)
    return filename