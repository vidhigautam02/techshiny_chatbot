import os
import logging
from dotenv import load_dotenv
import streamlit as st
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.chains import ConversationalRetrievalChain
from langchain_community.vectorstores import FAISS
import google.generativeai as genai
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import requests
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import faiss


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
load_dotenv()

# Check if required environment variables are set
if not os.getenv('GOOGLE_API_KEY'):
    raise ValueError("GOOGLE_API_KEY environment variable not set")



def scrape_website_content(url, depth=2):
    """
    Scrapes the website content from the given URL, including extracting text, image info, and links.
    Optionally extracts content from linked pages up to a specified depth.
    """

    def extract_content_from_soup(soup):
    # Extract headings (h1 - h6)
        headings = [heading.get_text(strip=True) for heading in soup.find_all(re.compile('^h[1-6]$'))]
        heading_text = "\n".join(headings)

    # Extract paragraphs
        paragraphs = [para.get_text(strip=True) for para in soup.find_all('p')]
        paragraph_text = "\n".join(paragraphs)

    # Extract list items from unordered and ordered lists
        list_items = [li.get_text(strip=True) for li in soup.find_all('li')]
        list_text = "\n".join(list_items)

    # **Updated**: Extract links and their text or icons (anchor tags)
        links = []
        for a_tag in soup.find_all('a', href=True):
        # Check for text in the <a> tag
            link_text = a_tag.get_text(strip=True)
        
        # If no text, check if it has an image or icon inside
            if not link_text:
            # Get the 'alt' attribute of images or icon classes
                images = a_tag.find_all('img')
                if images:
                    link_text = ', '.join([img.get('alt', 'Image without alt text') for img in images])
                else:
                # If there is an icon (e.g., a <span> or <i> tag for icons)
                    icons = a_tag.find_all(['span', 'i'])
                    if icons:
                        link_text = 'Icon link'
        
        # Append the link with either its text or description
            links.append((link_text, a_tag.get('href')))
    
        link_text = "\n".join([f"Link text: {text or 'No text'}, URL: {href}" for text, href in links])

    # Extract table data (table headers and cells)
        tables = []
        for table in soup.find_all('table'):
            headers = [header.get_text(strip=True) for header in table.find_all('th')]
            rows = []
            for row in table.find_all('tr'):
                rows.append([cell.get_text(strip=True) for cell in row.find_all(['td', 'th'])])
            tables.append({"headers": headers, "rows": rows})

        table_text = "\n".join(
            [f"Table {i+1}:\nHeaders: {', '.join(table['headers'])}\nRows:\n" + "\n".join([', '.join(row) for row in table['rows']]) 
             for i, table in enumerate(tables)]
        )

    # Extract meta descriptions
        meta_descriptions = [meta.get('content', '') for meta in soup.find_all('meta', {'name': 'description'})]
        meta_text = "\n".join(meta_descriptions)

    # Extract contact information (emails and phone numbers)
        contact_info = []
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', soup.get_text())
        phone_numbers = re.findall(r'\+?\d[\d -]{8,}\d', soup.get_text())
        contact_info.extend(emails)
        contact_info.extend(phone_numbers)
        contact_info_text = "\n".join(contact_info)

    # Extract alt text from images
        image_alts = [img.get('alt', '').strip() for img in soup.find_all('img') if img.get('alt')]
        image_alt_text = "\n".join(image_alts)

    # Combine everything into a single content string
        content = (
            f"Headings:\n{heading_text}\n\n"
            f"Paragraphs:\n{paragraph_text}\n\n"
            f"Lists:\n{list_text}\n\n"
            f"Links:\n{link_text}\n\n"
            f"Tables:\n{table_text}\n\n"
            f"Meta Descriptions:\n{meta_text}\n\n"
            f"Contact Info:\n{contact_info_text}\n\n"
            f"Image Alt Text:\n{image_alt_text}\n"
        )
        return content


    def scrape_recursive(url, depth):
        if depth < 0:
            return ""

        print(f"Scraping {url} at depth {depth}")
        content = ""

        try:
            response = requests.get(url)
            response.raise_for_status()  # Check for HTTP errors
            soup = BeautifulSoup(response.content, 'html.parser')

            # Extract content from the current page
            content += extract_content_from_soup(soup)

            if depth > 0:
                # Find and scrape linked pages
                links = set()  # Use a set to avoid duplicate links
                for a_tag in soup.find_all('a', href=True):
                    link = a_tag.get('href')
                    full_url = urljoin(url, link)  # Resolve relative URLs
                    if full_url.startswith('http') and not full_url.startswith(url):  # Avoid internal links if necessary
                        links.add(full_url)

                # Recursively scrape linked pages
                for link in links:
                    content += "\n\n---\n\n" + scrape_recursive(link, depth - 1)

        except requests.RequestException as e:
            print(f"Request error while scraping {url}: {e}")
 
        return content

    return scrape_recursive(url, depth)


def process_website(url):
    """
    Processes the website: scrapes text, splits it into chunks,
    and converts chunks into embeddings.
    """
    print(f"Processing website: {url}")
    text = scrape_website_content(url)

    # Split text into manageable chunks
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    chunks = text_splitter.split_text(text)
    print(f"Text split into {len(chunks)} chunks")

    chunks_with_sources = [(chunk, {"source": url}) for chunk in chunks]
    return chunks_with_sources

def upload_website_data(url):
    """
    Scrapes the website, processes the content, and saves it into a FAISS vector store.
    """
    print(f"Uploading website data for {url}")
    chunks_with_sources = process_website(url)
    if chunks_with_sources:
        text_chunks, metadata = zip(*chunks_with_sources)
        print(f"Creating embeddings for {len(text_chunks)} chunks")
        embeddings = GoogleGenerativeAIEmbeddings(api_key=os.getenv('GOOGLE_API_KEY'), model="models/text-embedding-004")
        vector_store = FAISS.from_texts(text_chunks, embedding=embeddings, metadatas=metadata)
        vector_store.save_local("faiss_index")
        print("FAISS index created or updated successfully.")
    else:
        print("No valid website data to process.")



    
def reframe_with_gemini(text,question):
    # Configure the API key from the environment variable
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    
    # Set up the generation configuration
    generation_config = {
        "temperature": 0.1,
        "max_output_tokens": 1200,

    }
    
    # Initialize the model
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config=generation_config,
    )
    
    # Prepare the prompt
    prompt = f"""You are a chatbot for an IT company website, tasked with answering user questions based on the provided website text. When responding, please:

1. Directly Address the Query: Use the website text to provide a clear and relevant answer to the user's question.
2. Be Empathetic and Polite: Offer a response in a natural, friendly manner.
3. Request More Specificity if Needed: If the query is not fully covered by the website text, gently request more details to provide a precise answer.
4. Encourage Further Consultation: If the answer is incomplete or if additional information might be needed, suggest that the user consult more resources for comprehensive details.
5. Avoid Irrelevant Information: Do not provide guesses or information not found in the website text. 
6. always provide the link of the related information not the company website link
7. add the required information from the images and links 
User Query: {question}

Website Text: {text}
"""


    # Generate the response
    try:
        response = model.generate_content(prompt)
        
        # Extract and return the content
        if hasattr(response, 'candidates') and len(response.candidates) > 0:
            return response.candidates[0].content.parts[0].text
        else:
            print("No candidates found in the response.")
            return None
    except Exception as e:
        print(f"Error occurred: {e}")
        return None

def generate_natural_language_response(relevant_info, question):
    """
    Generates a natural language response based on the relevant information.
    """
    if not relevant_info:
        return "Sorry, I couldn't find any relevant information."

    response = "Here's what I found based on your question:\n\n"
    
    # Aggregate and format the information
    aggregated_texts = []
    for text, _ in relevant_info:
        aggregated_texts.append(text)

    combined_text = " ".join(aggregated_texts)  # Combine all relevant texts
    summarized_text = reframe_with_gemini(combined_text, question)  # Reframe combined text
    response += summarized_text

    return response.strip()


def extract_relevant_information(question, text_chunks, metadata):
    """
    Extracts and aggregates relevant information based on the question.
    """
    relevant_info = []
    keywords = re.findall(r'\b\w+\b', question.lower())
    
    for chunk, meta in zip(text_chunks, metadata):
        chunk_lower = chunk.lower()
        if any(keyword in chunk_lower for keyword in keywords):
            relevant_info.append((chunk, meta))
    
    return relevant_info

def query(question, chat_history):
    """
    Processes a query using the conversational retrieval chain and returns a natural language response.
    """
    try:
        # Initialize embeddings and vector store
        embeddings = GoogleGenerativeAIEmbeddings(api_key=os.getenv('GOOGLE_API_KEY'), model="models/text-embedding-004")
        vector_store_path = "faiss_index"
        
        # Check if the FAISS index file exists
        if not os.path.exists(vector_store_path):
            raise FileNotFoundError(f"FAISS index file not found at path: {vector_store_path}")

        vector_store = FAISS.load_local(vector_store_path, embeddings=embeddings, allow_dangerous_deserialization=True)

        # Retrieve the relevant chunks based on the question
        search_results = vector_store.similarity_search(question)
        if not search_results:
            return {"answer": "I couldn't find any relevant information.", "sources": []}

        # Extract text and metadata from search results
        text_chunks = [result.page_content for result in search_results]
        metadata = [result.metadata for result in search_results]

        # Extract relevant information
        relevant_info = extract_relevant_information(question, text_chunks, metadata)

        # Generate a response using the reframed information
        formatted_answer = generate_natural_language_response(relevant_info, question) if relevant_info else "I couldn't find a specific answer. Could you please provide more details or ask a different question?"

        return {"answer": formatted_answer}

    except FileNotFoundError as e:
        logging.error(f"File error: {e}")
        return {"answer": "The resource could not be found. Please ensure the data has been correctly uploaded."}
    except Exception as e:
        logging.error(f"Error during query: {e}")
        return {"answer": "Oops, something went wrong while processing your query. Please try again later."}

import streamlit as st

import streamlit as st

import streamlit as st

def show_ui():
    """
    Sets up the Streamlit UI for TechShiney Digital Transformation Assistant with refined heading and white background for the logo.
    """
    # Add custom CSS to ensure the white background only surrounds the logo and to refine the heading style
    st.markdown("""
        <style>
        .logo-container {
            display: flex;
            justify-content: left;  /* Center the logo */
            margin-bottom: 20px;
        }
        .logo-container img {
            background-color: white;
            padding: 10px;
            border-radius: 10px;
            max-width: 300px;  /* Limit the logo's width */
            height: auto;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); /* Add a subtle shadow for emphasis */
        }
        .title {
            text-align: left;  /* Center the heading */
            font-size: 28px;     /* Adjust font size as needed */
            font-weight: bold;   /* Make the font bold */
            margin-bottom: 20px; /* Space below the heading */
        }
        </style>
        """, unsafe_allow_html=True)
    
    # Display the refined title and logo with a properly centered white background around the logo only
    st.markdown('<h1 class="title">TechShiney Digital Transformation Assistant</h1>', unsafe_allow_html=True)
    
    # Logo with centered white background
    st.markdown("""
    <div class="logo-container">
        <img src="https://techshiney.com/wp-content/uploads/2024/03/LOGO-Techshiney-1-9-2.png" alt="TechShiney Logo">
    </div>
    """, unsafe_allow_html=True)
    
    # Introductory text reflecting TechShiney's services
    st.write("""
        Welcome to TechShiney! How can we assist you in driving innovation today?
    """)
    
    # Initialize session state for chat history and messages
    if 'messages' not in st.session_state:
        st.session_state.messages = []
    if 'chat_history' not in st.session_state:
        st.session_state.chat_history = []

    # Display chat messages from history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # Accept user input
    if prompt := st.chat_input("Enter Your Query"):
        try:
            # Process the user's query and get the response
            with st.spinner("Processing your query..."):
                response = query(question=prompt, chat_history=st.session_state.chat_history)

                # Display the user's query and bot's response
                with st.chat_message("user"):
                    st.markdown(prompt)
                with st.chat_message("assistant"):
                    st.markdown(f"{response.get('answer', 'Sorry, I couldn\'t find an answer.')}")

                # Store the messages in the session state
                st.session_state.messages.append({"role": "user", "content": prompt})
                st.session_state.messages.append({"role": "assistant", "content": response.get('answer', 'Sorry, I couldn\'t find an answer.')})

                # Update chat history
                st.session_state.chat_history.append((prompt, response.get('answer', 'Sorry, I couldn\'t find an answer.')))
        except Exception as e:
            st.error(f"An error occurred while processing the query: {str(e)}")

    # Show the button only if there is at least one message in the chat history
    if st.session_state.messages:
        if st.button("Start New Conversation"):
            # Reset the chat state
            st.session_state.messages = []
            st.session_state.chat_history = []



if __name__ == "__main__":
    website_url = "https://techshiney.com/"  # Change to your desired website URL
    print("Starting to upload website data")
    upload_website_data(website_url)  # Scrape and process the website
    print("Launching Streamlit UI")
    show_ui()