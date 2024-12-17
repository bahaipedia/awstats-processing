import os
import argparse
import mysql.connector
from datetime import datetime
from urllib.parse import unquote
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Database credentials from .env
db_host = os.getenv('DB_HOST')
db_user = os.getenv('DB_USER')
db_password = os.getenv('DB_PASSWORD')
db_name = os.getenv('DB_NAME')

# Database connection
def get_database_connection():
    return mysql.connector.connect(
        host=db_host,
        user=db_user,
        password=db_password,
        database=db_name
    )

# Determine the path to the script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Path to the ignore_urls.txt file
IGNORE_URLS_FILE = os.path.join(SCRIPT_DIR, 'ignore_urls.txt')

# Load ignore patterns from the file
if os.path.exists(IGNORE_URLS_FILE):
    with open(IGNORE_URLS_FILE, 'r') as f:
        ignore_patterns = [line.strip() for line in f if line.strip()]
else:
    ignore_patterns = []

# Map directories to server IDs
def get_server_id(directory):
    server_mapping = {
        '/var/lib/awstats': 1,
        '/home/private/server_stats/frankfurt': 2,
        '/home/private/server_stats/saopaulo': 4,
        '/home/private/server_stats/singapore': 3
    }
    return server_mapping.get(directory)

# Get or create a website entry
def get_website_id(cursor, website_name):
    cursor.execute("SELECT id FROM websites WHERE name = %s", (website_name,))
    result = cursor.fetchone()
    if result:
        return result[0]
    else:
        raise ValueError(f"Website '{website_name}' not found in the database.")

# Check if the file has been processed
def has_file_been_processed(cursor, filename, server_id, last_modified, force):
    if force:
        return False
    cursor.execute("""
        SELECT last_modified FROM file_tracking
        WHERE filename = %s AND server_id = %s
    """, (filename, server_id))
    result = cursor.fetchone()
    return result and result[0] == last_modified

# Update file tracking table
def update_file_tracking(cursor, filename, server_id, last_modified):
    cursor.execute("""
        INSERT INTO file_tracking (filename, server_id, last_modified, processed_date)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE last_modified = VALUES(last_modified), processed_date = VALUES(processed_date)
    """, (filename, server_id, last_modified, datetime.now().replace(microsecond=0)))

# Check if a URL should be ignored
def should_ignore_url(url):
    return url in ["", "/"] or any(url.startswith(pattern) for pattern in ignore_patterns)

# Parse the BEGIN_MAP section to get positions
def parse_begin_map(file):
    positions = {}
    for line in file:
        line = line.decode('utf-8').strip()
        if line.startswith('BEGIN_MAP'):
            continue
        elif line.startswith('END_MAP'):
            break
        else:
            parts = line.split()
            if len(parts) == 2 and parts[0].startswith('POS_'):
                positions[parts[0]] = int(parts[1])
    return positions

# Parse POS_SIDER section
def parse_pos_sider(file, pos_sider_offset):
    file.seek(pos_sider_offset)
    url_data = []
    for line in file:
        line = line.decode('utf-8').strip()
        if line.startswith('END_SIDER'):
            break
        elif not line.startswith('#') and not line.startswith('BEGIN_SIDER'):
            parts = line.split()
            if len(parts) == 5:  # URL, Pages, Bandwidth, Entry, Exit
                url = parts[0]
                # Remove leading slash if present
                if url.startswith('/'):
                    url = url[1:]
                # Remove 'wiki/' prefix if present
                if url.startswith('wiki/'):
                    url = url[len('wiki/'):]
                # Decode URL
                url = unquote(url)
                # Now, check if the URL should be ignored
                if should_ignore_url(url):
                    continue
                pages, bandwidth, entry, exit_ = map(int, parts[1:])
                url_data.append({
                    'url': url,
                    'pages': pages,
                    'bandwidth': bandwidth,
                    'entry': entry,
                    'exit': exit_
                })
    return url_data

# Get or create a website_url entry
def get_or_create_website_url_id(cursor, website_id, url):
    cursor.execute("SELECT id FROM website_url WHERE website_id = %s AND url = %s", (website_id, url))
    result = cursor.fetchone()
    if result:
        return result[0]
    cursor.execute("INSERT INTO website_url (website_id, url) VALUES (%s, %s)", (website_id, url))
    return cursor.lastrowid

# Update server stats
def update_server_stats(cursor, website_url_id, server_id, year, month, data):
    cursor.execute("""
        INSERT INTO website_url_stats (website_url_id, server_id, year, month, hits, entry_count, exit_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
        hits = hits + VALUES(hits),
        entry_count = entry_count + VALUES(entry_count),
        exit_count = exit_count + VALUES(exit_count)
    """, (website_url_id, server_id, year, month, data['pages'], data['entry'], data['exit']))

# Process a single AWStats file
def process_file(cursor, file_path, server_id, force):
    filename = os.path.basename(file_path)
    last_modified = datetime.fromtimestamp(os.path.getmtime(file_path)).replace(microsecond=0)

    # Check if the file has already been processed
    if has_file_been_processed(cursor, filename, server_id, last_modified, force):
        print(f"File {filename} has already been processed.")
        return

    with open(file_path, 'rb') as file:
        # Parse BEGIN_MAP to get section positions
        positions = parse_begin_map(file)

        # Verify the required POS_SIDER section is available
        if 'POS_SIDER' not in positions:
            print(f"POS_SIDER section not found in {filename}")
            return

        # Parse the POS_SIDER section
        sider_data = parse_pos_sider(file, positions['POS_SIDER'])

    # Extract website name from filename
    website_name = '.'.join(filename.split('.')[1:-1])
    website_id = get_website_id(cursor, website_name)

    # Determine year and month from filename
    year = int(filename[9:13])
    month = int(filename[7:9])

    # Insert or update stats for each URL in POS_SIDER
    for data in sider_data:
        website_url_id = get_or_create_website_url_id(cursor, website_id, data['url'])
        update_server_stats(cursor, website_url_id, server_id, year, month, data)

    # Update the file tracking to mark it as processed
    update_file_tracking(cursor, filename, server_id, last_modified)
    print(f"Processed file {filename}.")

# Main function
def main():
    parser = argparse.ArgumentParser(description='Process AWStats sider data.')
    parser.add_argument('--server', type=str, help='Specify the server location')
    parser.add_argument('--file', type=str, help='Specify the file to process')
    parser.add_argument('--force', action='store_true', help='Force processing of the specified file')
    parser.add_argument('--website', type=str, help='Specify the website name')
    args = parser.parse_args()

    connection = get_database_connection()
    cursor = connection.cursor()

    directories = [
        '/var/lib/awstats',
        '/home/private/server_stats/frankfurt',
        '/home/private/server_stats/saopaulo',
        '/home/private/server_stats/singapore'
    ]

    if args.server:
        directories = [dir for dir in directories if args.server in dir]
        if not directories:
            print(f"No directory found for server '{args.server}'.")
            return

    # Determine website_id if --website is specified
    website_id = None
    if args.website:
        cursor.execute("SELECT id FROM websites WHERE name = %s", (args.website,))
        result = cursor.fetchone()
        if result:
            website_id = result[0]
        else:
            print(f"Website '{args.website}' not found in the database.")
            return

    # If force is True, delete existing data related to the website/server/file
    if args.force:
        if args.website:
            # Get the website_id if not already obtained
            cursor.execute("SELECT id FROM websites WHERE name = %s", (args.website,))
            result = cursor.fetchone()
            if result:
                website_id = result[0]
            else:
                print(f"Website '{args.website}' not found in the database.")
                return
            # Delete stats for the specified website
            cursor.execute("""
                DELETE ws FROM website_url_stats ws
                INNER JOIN website_url wu ON ws.website_url_id = wu.id
                WHERE wu.website_id = %s
            """, (website_id,))
            # Delete unused URLs for the website
            cursor.execute("""
                DELETE wu FROM website_url wu
                LEFT JOIN website_url_stats ws ON wu.id = ws.website_url_id
                WHERE wu.website_id = %s AND ws.website_url_id IS NULL
            """, (website_id,))
        if args.server:
            # Retrieve 'server_id' based on 'args.server'
            server_id = get_server_id_from_name(args.server)
            if server_id is None:
                print(f"Invalid server name '{args.server}'")
                return
            # Delete stats for the specified server
            cursor.execute("""
                DELETE FROM website_url_stats
                WHERE server_id = %s
            """, (server_id,))
            # Delete unused website_url entries
            cursor.execute("""
                DELETE wu FROM website_url wu
                LEFT JOIN website_url_stats ws ON wu.id = ws.website_url_id
                WHERE ws.website_url_id IS NULL
            """)
        if args.file:
            # Extract website_name, year, and month from args.file
            filename = args.file
            filename_without_extension = filename[:-4]  # Remove '.txt'
            parts = filename_without_extension.split('.')
            if len(parts) < 2:
                print(f"Invalid file name format '{args.file}'. Cannot extract website name.")
                return
            # Extract website name
            website_name = '.'.join(parts[1:])
            # Extract year and month from file name (assuming format 'awstatsMMYYYY')
            import re
            match = re.match(r'awstats(\d{2})(\d{4})', filename_without_extension)
            if match:
                month_str, year_str = match.groups()
                month = int(month_str)
                year = int(year_str)
            else:
                print(f"Invalid file name format '{args.file}'. Cannot extract year and month.")
                return
            # Get website_id
            cursor.execute("SELECT id FROM websites WHERE name = %s", (website_name,))
            result = cursor.fetchone()
            if result:
                website_id = result[0]
            else:
                print(f"Website '{website_name}' not found in the database.")
                return
            # Delete stats for the specified website, year, and month
            cursor.execute("""
                DELETE ws FROM website_url_stats ws
                INNER JOIN website_url wu ON ws.website_url_id = wu.id
                WHERE wu.website_id = %s AND ws.year = %s AND ws.month = %s
            """, (website_id, year, month))
            # Delete unused URLs for the website if they have no stats
            cursor.execute("""
                DELETE wu FROM website_url wu
                LEFT JOIN website_url_stats ws ON wu.id = ws.website_url_id
                WHERE wu.website_id = %s AND ws.website_url_id IS NULL
            """, (website_id,))
        if not args.website and not args.server and not args.file:
            # Delete all stats and URLs
            cursor.execute("DELETE FROM website_url_stats")
            cursor.execute("DELETE FROM website_url")       

    for directory in directories:
        server_id = get_server_id(directory)
        if server_id is None:
            print(f"Server ID not found for directory '{directory}'.")
            continue

        if args.file:
            file_path = os.path.join(directory, args.file)
            if os.path.exists(file_path):
                process_file(cursor, file_path, server_id, args.force)
            else:
                print(f"File '{args.file}' not found in directory '{directory}'.")
        else:
            for filename in os.listdir(directory):
                if filename.endswith('.txt') and 'awstats' in filename:
                    if args.website:
                        filename_without_extension = filename[:-4]
                        website_part = filename_without_extension[13+1:]
                        if website_part != args.website:
                            continue
                    file_path = os.path.join(directory, filename)
                    process_file(cursor, file_path, server_id, args.force)

    connection.commit()
    cursor.close()
    connection.close()

if __name__ == "__main__":
    main()