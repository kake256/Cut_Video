import os
from yt_dlp import YoutubeDL

def sanitize_filename(filename, max_length=100):
    """ファイル名の長さを制限し、特殊文字を削除"""
    invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '#']
    for char in invalid_chars:
        filename = filename.replace(char, '_')  # 禁止文字をアンダースコアに置換
    return filename[:max_length]  # 最大100文字までに制限

def download_youtube_video_as_mp4(url, save_path="./"):
    try:
        if not os.path.exists(save_path):
            os.makedirs(save_path)

        # yt-dlpのオプションを設定
        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
            'outtmpl': os.path.join(save_path, '%(id)s.%(ext)s'),  # 動画IDをファイル名に
            'noplaylist': True,  # プレイリストを無視
            'retries': 21,  # 最大3回リトライ
        }

        with YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            video_title = sanitize_filename(info_dict.get('title', 'video'))
            video_id = info_dict.get('id', 'unknown')
            video_ext = info_dict.get('ext', 'mp4')

            # ダウンロードしたファイルのパス
            old_path = os.path.join(save_path, f"{video_id}.{video_ext}")
            new_path = os.path.join(save_path, f"{video_title}.{video_ext}")
            os.rename(old_path, new_path)

        print(f"Download completed! Saved as: {new_path}")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    video_url = input("Enter the YouTube video URL: ").strip()
    save_directory = input(
        "Enter the directory to save the video (leave blank for current directory): "
    ).strip() or "./"

    print(f"Downloading video from: {video_url}")
    download_youtube_video_as_mp4(video_url, save_path=save_directory)
