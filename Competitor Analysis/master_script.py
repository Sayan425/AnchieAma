import os
import json
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from apify_client import ApifyClient
import dateparser
from datetime import datetime, timezone
import statistics
import time
from groq import Groq
from pydantic import BaseModel

def get_user_inputs():
    print("--- CONTENT PLANNER CONFIGURATION ---")
    avatar = input("1. Which avatar do you want to run this for? ")
    timeperiod = input("2. Timeperiod of analysis? (e.g. 1 day, 2 weeks, 3 months, 1 year): ")
    print("-" * 37 + "\n")
    return avatar.strip(), timeperiod.strip()

def get_watchlist_data(target_avatar):
    """
    Connects to Google Sheets using the local service account key
    and pulls data from the Watchlist tab, filtering by Avatar.
    """
    load_dotenv()

    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    watchlist_tab_name = os.getenv("WATCHLIST_TAB", "Watchlist")

    if not spreadsheet_id:
        print("ERROR: SPREADSHEET_ID not found in .env file.")
        exit(1)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    credentials_file = "Sheets_service_account_key.json"

    if not os.path.exists(credentials_file):
        print(f"ERROR: Could not find {credentials_file}")
        exit(1)

    print("Authenticating with Google Sheets...")
    credentials = Credentials.from_service_account_file(credentials_file, scopes=scopes)
    gc = gspread.authorize(credentials)

    print(f"Opening Spreadsheet ID: {spreadsheet_id}")
    try:
        sh = gc.open_by_key(spreadsheet_id)
        worksheet = sh.worksheet(watchlist_tab_name)
    except gspread.exceptions.APIError as e:
        print(f"Google Sheets API Error: {e}")
        exit(1)
    except gspread.exceptions.WorksheetNotFound:
        print(f"ERROR: Could not find a tab named '{watchlist_tab_name}'")
        exit(1)

    print(f"Pulling data from '{watchlist_tab_name}' tab...")

    all_records = worksheet.get_all_records()
    filtered_records = [r for r in all_records if str(r.get("Avatar", "")).strip() == target_avatar]

    print(f"Successfully pulled {len(all_records)} total creators from the watchlist.")
    print(f"Filtered down to {len(filtered_records)} creators matching Avatar: '{target_avatar}'\n")
    return filtered_records

def run_apify_actor(creators_data, timeperiod):
    """
    Triggers the Apify instagram-reel-scraper with exact config parameters.
    Saves and returns the output as output.json.
    """
    apify_token = os.getenv("APIFY_TOKEN")
    if not apify_token:
        print("ERROR: APIFY_TOKEN not found in .env file.")
        exit(1)

    client = ApifyClient(apify_token)

    ig_urls = [row.get("Creator Instagram Handle") for row in creators_data if row.get("Creator Instagram Handle")]

    if not ig_urls:
        print("No Instagram URLs found in the filtered data!")
        return []

    print(f"Starting Apify Actor (apify/instagram-reel-scraper) for {len(ig_urls)} profiles...")
    print(f"Server-side Timeperiod Cutoff: {timeperiod}")

    run_input = {
        "includeDownloadedVideo": False,
        "includeSharesCount": False,
        "includeTranscript": False,
        "onlyPostsNewerThan": timeperiod,
        "resultsLimit": 30,
        "skipPinnedPosts": True,
        "username": ig_urls,
    }

    print("Waiting for Apify to scrape... (This might take a few minutes)")
    run = client.actor("apify/instagram-reel-scraper").call(run_input=run_input)

    dataset_id = getattr(run, 'default_dataset_id', getattr(run, 'defaultDatasetId', None))
    if not dataset_id and hasattr(run, 'model_dump'):
        dataset_id = run.model_dump().get("defaultDatasetId", run.model_dump().get("default_dataset_id"))
    elif not dataset_id and isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId")

    print(f"Apify run finished! Fetching dataset items from {dataset_id}...")

    cleaned_reels = []
    for item in client.dataset(dataset_id).iterate_items():
        timestamp = item.get("timestamp", "")
        date_only = timestamp[:10] if timestamp else ""

        cleaned_item = {
            "caption": item.get("caption", ""),
            "url": item.get("url"),
            "videourl": item.get("videoUrl"),
            "commentCount": item.get("commentsCount"),
            "likesCount": item.get("likesCount"),
            "Date": date_only,
            "Creator": item.get("ownerUsername"),
            "audioUrl": item.get("audioUrl", ""),
            "videoPlayCount": item.get("videoPlayCount"),
            "videoViewCount": item.get("videoViewCount")
        }
        cleaned_reels.append(cleaned_item)

    print(f"Fetched and cleaned {len(cleaned_reels)} reels that were posted in the last '{timeperiod}'!")

    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(cleaned_reels, f, indent=4, ensure_ascii=False)

    print("Saved output to output.json\n")
    return cleaned_reels

def process_metrics(creators_data, reels_data):
    """
    Calculates median views and reach efficiency for each creator.
    Updates the Google Sheet directly.
    """
    print("--- STEP 4: METRICS PROCESSING ---")

    if not reels_data:
        print("No reels data to process.")
        return

    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    watchlist_tab_name = os.getenv("WATCHLIST_TAB", "Watchlist")

    credentials_file = "Sheets_service_account_key.json"
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = Credentials.from_service_account_file(credentials_file, scopes=scopes)
    gc = gspread.authorize(credentials)

    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.worksheet(watchlist_tab_name)

    all_records = worksheet.get_all_records()

    for creator in creators_data:
        ig_handle = creator.get("Creator Instagram Handle", "")
        handle_username = ig_handle.rstrip("/").split("/")[-1]

        creator_reels = [r for r in reels_data if r.get("Creator") == handle_username]

        if not creator_reels:
            print(f"No reels found for {handle_username} in the given timeperiod.")
            continue

        views = [r.get("videoPlayCount", 0) for r in creator_reels if r.get("videoPlayCount") is not None]
        if not views:
            print(f"[{handle_username}] No play count data available from Instagram — skipping metrics.")
            continue

        median_views = statistics.median(views)

        follower_count = creator.get("Follower Count")
        try:
            follower_count = float(str(follower_count).replace(",", ""))
            if follower_count > 0:
                reach_efficiency = (median_views / follower_count) * 100
            else:
                reach_efficiency = 0
        except (ValueError, TypeError):
            reach_efficiency = 0

        print(f"[{handle_username}] Median Views: {median_views} | Reach Efficiency: {reach_efficiency:.2f}%")

        for r in creator_reels:
            r["creatorMedianReach"] = median_views
            r["creatorFollowerCount"] = follower_count
            r["creatorReachEfficiency"] = reach_efficiency

        row_index = None
        for i, rec in enumerate(all_records):
            if rec.get("Creator Instagram Handle") == ig_handle:
                row_index = i + 2
                break

        if row_index:
            worksheet.update_cell(row_index, 5, median_views)
            time.sleep(1)
            worksheet.update_cell(row_index, 6, f"{reach_efficiency:.2f}%")
            time.sleep(1)

    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(reels_data, f, indent=4, ensure_ascii=False)

    print("Metrics processing complete! Google Sheet updated and output.json injected with creator metrics.\n")

def transcribe_reels(reels_data):
    """
    Transcribes the audio/video URL of each reel using Groq Whisper API.
    Retries up to 3 times on failure before giving up on a reel.
    Updates the reels in-place and saves back to output.json.
    """
    print("--- STEP 5: CLOUD TRANSCRIPTION ---")
    load_dotenv()
    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        print("ERROR: GROQ_API_KEY not found in .env file.")
        return reels_data

    client = Groq()

    for idx, reel in enumerate(reels_data):
        media_url = reel.get("audioUrl")
        if not media_url:
            media_url = reel.get("videourl")

        if not media_url:
            print(f"[{idx+1}/{len(reels_data)}] SKIPPED — No audio or video URL returned by Apify for this reel.")
            reel["Transcript"] = ""
            continue

        print(f"[{idx+1}/{len(reels_data)}] Transcribing reel from @{reel.get('Creator')}...")

        transcript = ""
        success = False
        for attempt in range(1, 4):
            try:
                transcription = client.audio.transcriptions.create(
                    url=media_url,
                    model="whisper-large-v3",
                    temperature=0,
                    response_format="verbose_json",
                )
                transcript = transcription.text
                print(f"  -> SUCCESS on attempt {attempt}. Transcript length: {len(transcript)} chars")
                success = True
                break
            except Exception as e:
                print(f"  -> Attempt {attempt}/3 FAILED: {e}")
                if attempt < 3:
                    print(f"  -> Retrying in 8 seconds...")
                    time.sleep(8)

        if not success:
            print(f"  -> All 3 transcription attempts failed. Reel will be skipped in AI analysis.")

        reel["Transcript"] = transcript

        if idx < len(reels_data) - 1:
            print("Waiting 8 seconds before next transcription...")
            time.sleep(8)

    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(reels_data, f, indent=4, ensure_ascii=False)

    print("Cloud transcription complete! All transcripts saved to output.json.\n")
    return reels_data

def calculate_reel_scores(reels_data):
    """
    Calculates Engagement Score and Virality Score for each reel and injects them into output.json.
    Engagement Score = ((likes + 3 * comments) / views) * 100
    Virality Score = (plays / median_plays) * 100
    """
    print("--- STEP 5.5: CALCULATING REEL SCORES ---")
    for reel in reels_data:
        likes = reel.get("likesCount", 0) or 0
        comments = reel.get("commentCount", reel.get("commentsCount", 0)) or 0
        plays = reel.get("videoPlayCount", 0) or 0
        views = reel.get("videoViewCount", 0) or 0
        median_plays = reel.get("creatorMedianReach", 0) or 0

        if views > 0:
            engagement_score = ((likes + (3 * comments)) / views) * 100
        else:
            engagement_score = 0

        if median_plays > 0:
            virality_score = (plays / median_plays) * 100
        else:
            virality_score = 0

        reel["engagementScore"] = round(engagement_score, 2)
        reel["viralityScore"] = round(virality_score, 2)

    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(reels_data, f, indent=4, ensure_ascii=False)

    print("Reel scores calculated and injected into output.json!\n")
    return reels_data

class ReelAnalysis(BaseModel):
    is_relevant: bool
    reason: str = ""
    topic: str = ""
    take: str = ""
    storytelling_flow: str = ""
    hook_template: str = ""
    cta: str = ""

def analyze_reel_script(user_message: str, client: Groq) -> ReelAnalysis:
    system_prompt = """You are a viral reel script analyst helping a content creator reverse-engineer successful reels for children's brand marketing inspiration.

You will receive a Caption and a Transcript of an Instagram reel.

────────────────────────────
STEP 1 — LANGUAGE & READABILITY CHECK
────────────────────────────
The transcript may be in Hindi, English, or a mix of both (Hinglish).

Look at both the caption and the transcript together. Judge comprehensibility based on the overall meaning you can extract from both combined.

If the majority of both the caption and transcript are gibberish, incoherent, heavily distorted, or in an unrecognised language — and you cannot understand what the reel is broadly about — set is_relevant to false and fill in reason with: "Content is largely incomprehensible even with caption context." Leave all other fields as empty strings.

Note: It is normal for the tail end of a transcript to have some garbled or distorted words due to audio quality. Do not reject a reel for a few garbled lines at the end if the rest of the content is clearly understandable.

────────────────────────────
STEP 2 — RELEVANCE & RECREATABILITY CHECK
────────────────────────────
If the content passes Step 1, check:

Does this reel have any narrative, opinion, story, or point of view that could be adapted for a different topic or brand?

The bar here is very low — pass almost everything. Only reject if:
- The reel is a pure direct product advertisement with no story, no opinion, and no reusable narrative structure (e.g. "Buy our product now, link in bio")

If it has a hook, a take, a lesson, a story, an opinion, or any kind of structured narrative — even loosely — set is_relevant to true and proceed to Step 3.

If it truly is nothing but a direct ad with zero narrative value, set is_relevant to false and fill in reason with a clear explanation. Leave all other fields as empty strings.

────────────────────────────
STEP 3 — FULL ANALYSIS (only if is_relevant = true)
────────────────────────────
If the reel passes both checks, set is_relevant to true, leave reason as an empty string, and fill in all five fields below.

TOPIC
The subject of the reel in 5–6 words maximum.
Example: Why daily posting kills your growth

TAKE
The creator's unique spin, contrarian opinion, or fresh perspective on the topic — 1 crisp sentence.
Example: Consistency alone is not enough, quality of engagement matters more.

STORYTELLING FLOW
How the narration moves from start to finish. Break it into 4–6 stages, each described in 3–5 words, separated by →.
Example: Cute Doll → False Standards → Kids Compare → Confidence Drops → Healthier Toys

HOOK TEMPLATE
Take the opening hook of the reel exactly as it is spoken or written — do not modify it.
Example: Nobody tells you that posting daily actually kills your growth

CTA
If a call to action is present (spoken, implied, or gestured), summarise it in 4–5 words. If none exists, write: No CTA detected.

────────────────────────────
OUTPUT FORMAT
────────────────────────────
Always return a single valid JSON object with exactly these seven fields. No extra commentary outside the JSON. Inside any string value do not use any quotes or brackets.

{
  "is_relevant": true or false,
  "reason": "why this was rejected, or empty string if accepted",
  "topic": "the topic, or empty string if rejected",
  "take": "the take, or empty string if rejected",
  "storytelling_flow": "the flow, or empty string if rejected",
  "hook_template": "the opening hook exactly as spoken, or empty string if rejected",
  "cta": "the cta, or empty string if rejected"
}
"""

    completion = client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        temperature=1,
        max_completion_tokens=8192,
        top_p=1,
        stream=False,
        response_format={"type": "json_object"},
        stop=None
    )

    result_json = completion.choices[0].message.content
    return ReelAnalysis.model_validate_json(result_json)

def run_llm_analysis(reels_data):
    """
    Passes each transcript + caption to the AI model for validation and analysis.
    Retries up to 3 times if the AI returns a bad format.
    Outputs the final clean dataset ready for Google Sheets.
    """
    print("--- STEP 6: AI ANALYSIS ---")

    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        print("ERROR: GROQ_API_KEY not found in .env file.")
        return []

    client = Groq()
    final_results = []
    is_first_request = True

    for idx, reel in enumerate(reels_data):
        script = reel.get("Transcript", "")
        caption = reel.get("caption", "")

        if not script.strip() and not caption.strip():
            print(f"[{idx+1}/{len(reels_data)}] SKIPPED — No transcript or caption available for reel from @{reel.get('Creator')}.")
            continue

        if not script.strip():
            print(f"[{idx+1}/{len(reels_data)}] NOTE — No transcript for reel from @{reel.get('Creator')}. Sending caption only.")

        if not is_first_request:
            print("Waiting 15 seconds to avoid API rate limits...")
            time.sleep(15)
        is_first_request = False

        user_message = f"Caption: {caption}\n\nTranscript: {script}"

        print(f"[{idx+1}/{len(reels_data)}] Analyzing reel by @{reel.get('Creator')}...")

        analysis = None
        for attempt in range(1, 4):
            try:
                analysis = analyze_reel_script(user_message, client)
                break
            except Exception as e:
                print(f"  -> Attempt {attempt}/3 FAILED — AI returned bad format or error: {e}")
                if attempt < 3:
                    print(f"  -> Retrying in 15 seconds...")
                    time.sleep(15)

        if analysis is None:
            print(f"  -> All 3 AI attempts failed. Skipping this reel.")
            continue

        if not analysis.is_relevant:
            print(f"  -> REJECTED. Reason: {analysis.reason}")
            continue

        print(f"  -> ACCEPTED.")
        print(f"  -> Topic: {analysis.topic}")

        cleaned_item = {
            "Date of Posting": reel.get("Date", ""),
            "Creator Name": reel.get("Creator", ""),
            "Post Link": reel.get("url", ""),
            "Views": reel.get("videoViewCount", 0),
            "Plays": reel.get("videoPlayCount", 0),
            "Likes": reel.get("likesCount", 0),
            "Comments": reel.get("commentCount", reel.get("commentsCount", 0)),
            "Engagement Score": f"{reel.get('engagementScore', 0):.2f}%",
            "Virality Score": f"{reel.get('viralityScore', 0):.2f}%",
            "Transcript": script,
            "Topic": analysis.topic,
            "Take for the topic": analysis.take,
            "Hook Format": analysis.hook_template,
            "Story Telling Flow": analysis.storytelling_flow,
            "CTA": analysis.cta
        }

        final_results.append(cleaned_item)

    with open("analyzed_results.json", "w", encoding="utf-8") as f:
        json.dump(final_results, f, indent=4, ensure_ascii=False)

    print(f"AI analysis complete! Kept {len(final_results)} relevant reels. Saved to analyzed_results.json.\n")
    return final_results

def export_to_google_sheets(final_data, target_avatar):
    """
    Exports the final analyzed data back to the Google Sheet.
    """
    print("--- STEP 7: GOOGLE SHEETS EXPORT ---")
    if not final_data:
        print("No data to export.")
        return

    try:
        credentials = Credentials.from_service_account_file(
            "Sheets_service_account_key.json",
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(credentials)

        spreadsheet_id = os.getenv("SPREADSHEET_ID")
        export_tab_name = f"Idea Bank {target_avatar}"

        try:
            worksheet = gc.open_by_key(spreadsheet_id).worksheet(export_tab_name)
        except gspread.exceptions.WorksheetNotFound:
            print(f"❌ ERROR: Tab '{export_tab_name}' not found in the Google Sheet.")
            print(f"Please create a tab exactly named '{export_tab_name}' and run again.")
            return

        print(f"Connected to Google Sheet! Pushing {len(final_data)} rows to '{export_tab_name}'...")

        rows_to_insert = []
        for item in final_data:
            row = [
                item.get("Date of Posting", ""),
                item.get("Creator Name", ""),
                item.get("Post Link", ""),
                item.get("Views", 0),
                item.get("Plays", 0),
                item.get("Likes", 0),
                item.get("Comments", 0),
                item.get("Engagement Score", "0%"),
                item.get("Virality Score", "0%"),
                item.get("Transcript", ""),
                item.get("Topic", ""),
                item.get("Take for the topic", ""),
                item.get("Hook Format", ""),
                item.get("Story Telling Flow", ""),
                item.get("CTA", "")
            ]
            rows_to_insert.append(row)

        worksheet.append_rows(rows_to_insert, value_input_option='USER_ENTERED')

        print(f"✅ Successfully exported {len(rows_to_insert)} reels to Google Sheets!\n")

    except Exception as e:
        print(f"❌ Error exporting to Google Sheets: {e}")

if __name__ == "__main__":
    print("--- STARTING MASTER PIPELINE ---\n")

    target_avatar, analysis_timeperiod = get_user_inputs()

    data = get_watchlist_data(target_avatar)

    for i, row in enumerate(data):
        print(f"Row {i+1}:")
        for key, value in row.items():
            print(f"  {key}: {value}")
        print("-" * 30)

    if data:
        reels_data = run_apify_actor(data, analysis_timeperiod)

        if reels_data:
            process_metrics(data, reels_data)

        if reels_data:
            reels_data = transcribe_reels(reels_data)

        if reels_data:
            reels_data = calculate_reel_scores(reels_data)

        if reels_data:
            final_data = run_llm_analysis(reels_data)

        if 'final_data' in locals() and final_data:
            export_to_google_sheets(final_data, target_avatar)

    else:
        print("No creators found for this avatar. Skipping Apify scrape.")
